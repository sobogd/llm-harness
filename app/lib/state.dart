import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api.dart';
import 'model.dart';

enum ConnState { connecting, ok, down, badToken }

class AgentApp extends ChangeNotifier {
  final HarnessApi api = HarnessApi();
  final List<ViewMsg> messages = [];
  ViewMsg? live;
  ConnState conn = ConnState.connecting;
  bool harnessDown = false;
  bool compacting = false;
  HarnessStatus? status;
  final List<SessionInfo> sessions = [];
  bool sessionsError = false;
  String? toast;

  int _lastSeq = 0;
  DateTime _lastFrame = DateTime.now();
  SharedPreferences? _prefs;
  Timer? _stall;
  Timer? _toastTimer;
  Timer? _resyncTimer;
  int _backoff = 1;
  bool _connecting = false;
  bool _disposed = false;

  static const _stallSecs = 45;

  @override
  void dispose() {
    _disposed = true;
    _stall?.cancel();
    _toastTimer?.cancel();
    _resyncTimer?.cancel();
    api.dispose();
    super.dispose();
  }

  Future<void> init() async {
    _prefs = await SharedPreferences.getInstance();
    _lastSeq = _prefs!.getInt('last_seq') ?? 0;
    final saved = _prefs!.getString('auth_token') ?? '';
    if (saved.isNotEmpty) api.token = saved;
    await _resync();
    refreshSessions();
    _connect();
  }

  void notify() => notifyListeners();

  void showToast(String msg) {
    toast = msg;
    _toastTimer?.cancel();
    _toastTimer = Timer(const Duration(seconds: 5), () {
      toast = null;
      notify();
    });
    notify();
  }

  Future<void> _resync() async {
    try {
      final j = await api.status();
      status = HarnessStatus.fromMap(j);
      _connOk();
    } on AuthException {
      conn = ConnState.badToken;
      notify();
      return;
    } catch (_) {
      conn = ConnState.down;
      notify();
      return;
    }
    try {
      final ms = await api.messages();
      final list = ms.map(_viewFromJson).toList();
      final byCallId = <String, ViewMsg>{};
      for (final v in list) {
        if (v.role == 'tool' && v.toolCallId != null) {
          byCallId[v.toolCallId!] = v;
        }
      }
      for (final v in list) {
        for (final c in v.tools) {
          final r = byCallId[c.id];
          if (r != null) {
            c.resultPreview = r.text;
            c.ok = !r.text.startsWith('error:');
          }
        }
      }
      messages
        ..clear()
        ..addAll(list.where((v) => v.role == 'user' || v.role == 'assistant'));
      live = null;
      _connOk();
    } on AuthException {
      conn = ConnState.badToken;
    } catch (_) {
      conn = ConnState.down;
    }
    notify();
  }

  void _connOk() {
    if (conn != ConnState.badToken) conn = ConnState.ok;
  }

  ViewMsg _viewFromJson(Map<String, dynamic> m) {
    final role = m['role'] as String? ?? 'user';
    final content = m['content'] as String? ?? '';
    final thinking = m['reasoning_content'] as String? ?? '';
    final parts = <Object>[];
    if (role == 'assistant' && thinking.isNotEmpty) {
      parts.add(Part(PartKind.thinking, thinking));
    }
    if (content.isNotEmpty) {
      parts.add(Part(PartKind.text, content));
    }
    if (role == 'assistant') {
      for (final t in (m['tool_calls'] as List? ?? const [])) {
        final tc = t as Map<String, dynamic>;
        parts.add(
          ToolCard(
            id: (tc['id'] ?? '') as String,
            name: (tc['name'] ?? '') as String,
            argsPreview: tc['arguments'] as String?,
          ),
        );
      }
    }
    return ViewMsg(
      role: role,
      toolCallId: m['tool_call_id'] as String?,
      parts: parts,
    );
  }

  Future<void> _connect() async {
    if (_disposed || _connecting) return;
    if (conn == ConnState.badToken) return;
    _connecting = true;
    conn = ConnState.connecting;
    notify();
    var done = Completer<void>();
    try {
      final sub = api
          .events(since: _lastSeq)
          .listen(
            _onEvent,
            onError: (Object e) {},
            onDone: () {
              if (!done.isCompleted) done.complete();
            },
          );
      _stall?.cancel();
      _lastFrame = DateTime.now();
      _stall = Timer.periodic(const Duration(seconds: 15), (t) {
        if (DateTime.now().difference(_lastFrame) >=
            const Duration(seconds: _stallSecs)) {
          t.cancel();
          sub.cancel();
          if (!done.isCompleted) done.complete();
        }
      });
    } catch (_) {}
    await done.future;
    _stall?.cancel();
    _connecting = false;
    if (_disposed) return;
    conn = ConnState.down;
    notify();
    _resync();
    final wait = _backoff;
    _backoff = (_backoff * 2).clamp(1, 30);
    await Future<void>.delayed(Duration(seconds: wait));
    _connect();
  }

  void _persistSeq() {
    _prefs?.setInt('last_seq', _lastSeq);
  }

  void _scheduleResync() {
    _resyncTimer?.cancel();
    _resyncTimer = Timer(const Duration(seconds: 2), _resync);
  }

  void _onEvent(SseEvent ev) {
    final d = ev.data;
    final type = (d['type'] ?? ev.event) as String? ?? 'message';
    if (type == 'bridge') {
      harnessDown = d['kind'] == 'harness_down';
      notify();
      return;
    }
    _lastFrame = DateTime.now();
    if (type == 'keepalive') return;
    final seq = (d['seq'] as int?) ?? ev.id ?? 0;
    if (type == 'session_loaded') {
      _lastSeq = 0;
      _persistSeq();
      _resync();
      refreshSessions();
      return;
    }
    if (seq > 0) {
      if (seq <= _lastSeq) return;
      if (seq > _lastSeq + 1) {
        _lastSeq = 0;
        _persistSeq();
        _resync();
      }
      _lastSeq = seq;
      _persistSeq();
    }
    _connOk();
    switch (type) {
      case 'run_started':
      case 'run_resumed':
        live = ViewMsg(role: 'assistant');
        _stateFromStatusString('running');
      case 'queued':
      case 'turn_started':
      case 'thinking_delta':
      case 'text_delta':
      case 'tool_start':
      case 'tool_end':
      case 'usage':
      case 'turn_finished':
        _applyLive(d);
      case 'error':
        live ??= ViewMsg(role: 'assistant');
        live!.error = '${d['code'] ?? 'error'}: ${d['message'] ?? ''}';
      case 'run_done':
      case 'run_stopped':
        _stateFromStatusString(d['state'] as String?);
        _commitLive(d);
      case 'session_reset':
        messages.clear();
        live = null;
        _resync();
        refreshSessions();
      case 'settings_changed':
        _scheduleResync();
      case 'compact_started':
        compacting = true;
      case 'compact_done':
        compacting = false;
        showToast('compact: ${d['tokens_before']} → ${d['tokens_after']}');
        _scheduleResync();
      default:
        break;
    }
    notify();
  }

  void _applyLive(Map<String, dynamic> d) {
    switch (d['type']) {
      case 'thinking_delta':
        live ??= ViewMsg(role: 'assistant');
        live!.appendDelta(PartKind.thinking, (d['text'] ?? '') as String);
      case 'text_delta':
        live ??= ViewMsg(role: 'assistant');
        live!.appendDelta(PartKind.text, (d['text'] ?? '') as String);
      case 'tool_start':
        live ??= ViewMsg(role: 'assistant');
        live!.addTool(
          ToolCard(
            id: (d['tool_call_id'] ?? '') as String,
            name: (d['tool'] ?? '') as String,
            argsPreview: d['args_preview'] as String?,
          ),
        );
      case 'tool_end':
        final card = live?.toolById((d['tool_call_id'] ?? '') as String);
        if (card != null) {
          card.resultPreview = d['result_preview'] as String?;
          card.ok = (d['ok'] ?? false) as bool;
        }
      case 'usage':
        final u = d['usage'];
        if (u is Map) {
          live?.usage = Usage.fromJson(u.cast<String, dynamic>());
        }
      default:
        break;
    }
  }

  void _commitLive(Map<String, dynamic> d) {
    final l = live;
    live = null;
    if (l == null) return;
    if (l.text.isEmpty &&
        l.thinking.isEmpty &&
        l.tools.isEmpty &&
        l.error == null) {
      return;
    }
    if (l.error != null) {
      l.error = '${l.error} ${d['error'] ?? ''}'.trim();
    }
    messages.add(l);
    _scheduleResync();
  }

  void _stateFromStatusString(String? s) {
    switch (s) {
      case 'running':
        status = _replaceState(status, RunState.running);
      case 'stopped':
        status = _replaceState(status, RunState.stopped);
      case 'done':
        status = _replaceState(status, RunState.done);
      default:
        break;
    }
  }

  HarnessStatus? _replaceState(HarnessStatus? cur, RunState st) {
    if (cur == null) return null;
    return HarnessStatus(
      settings: cur.settings,
      state: st,
      turn: cur.turn,
      lastError: cur.lastError,
      sessionId: cur.sessionId,
      historyMessages: cur.historyMessages,
      promptTokensLast: cur.promptTokensLast,
      activeSubagents: cur.activeSubagents,
    );
  }

  Future<void> send(String text) async {
    if (text.trim().isEmpty) return;
    try {
      final j = await api.ask(text.trim());
      conn = ConnState.ok;
      messages.add(ViewMsg.user(text.trim()));
      live = ViewMsg(role: 'assistant');
      if (j['state'] == 'queued') {
        showToast('выполнится после текущего хода');
      }
      notify();
    } on AuthException {
      conn = ConnState.badToken;
      notify();
    } catch (e) {
      showToast('не удалось отправить: $e');
    }
  }

  Future<void> stop() async {
    try {
      await api.stop();
      _resync();
    } catch (e) {
      showToast('stop: $e');
    }
  }

  Future<void> resume() async {
    try {
      await api.resume();
      _resync();
    } catch (e) {
      showToast('resume: $e');
    }
  }

  Future<void> newSession() async {
    try {
      await api.newSession();
      messages.clear();
      live = null;
      showToast('новая сессия');
      await _resync();
      refreshSessions();
    } on AuthException {
      conn = ConnState.badToken;
      notify();
    } catch (e) {
      showToast('новая сессия: $e');
    }
  }

  Future<void> refreshSessions() async {
    final wasError = sessionsError;
    try {
      final j = await api.listSessions();
      sessions
        ..clear()
        ..addAll(
          (j['sessions'] as List? ?? const [])
              .map((e) => (e as Map).cast<String, dynamic>())
              .map(SessionInfo.fromJson),
        );
      sessionsError = false;
      notify();
    } on AuthException {
      conn = ConnState.badToken;
      notify();
    } catch (e) {
      // Не глотаем ошибку молча: иначе пустой список выглядит
      // как «пока пусто», хотя сервер просто не отвечает.
      sessionsError = true;
      if (!wasError) {
        showToast('не удалось загрузить список сессий: $e');
      }
      notify();
    }
  }

  Future<void> loadSession(String id) async {
    if (status != null && status!.state == RunState.running) {
      showToast('остановите запуск перед переключением сессии');
      return;
    }
    try {
      await api.loadSession(id);
      messages.clear();
      live = null;
      notify();
      await refreshSessions();
      await _resync();
    } on AuthException {
      conn = ConnState.badToken;
      notify();
    } catch (e) {
      showToast('сессия: $e');
    }
  }

  Future<void> deleteSession(String id) async {
    try {
      final wasActive = status?.sessionId == id;
      await api.deleteSession(id);
      if (wasActive) {
        // сервер сам создал новую сессию — чистим экран
        messages.clear();
        live = null;
      }
      await refreshSessions();
      await _resync();
    } on AuthException {
      conn = ConnState.badToken;
      notify();
    } catch (e) {
      showToast('удаление: $e');
    }
  }

  String? get activeSessionId => status?.sessionId;
  bool get busy => status?.state == RunState.running;

  String get savedToken => _prefs?.getString('auth_token') ?? '';

  Future<void> saveToken(String text) async {
    final v = text.trim();
    if (v == api.token) return;
    await _prefs!.setString('auth_token', v);
    api.token = v;
    if (conn == ConnState.badToken) {
      conn = ConnState.connecting;
      notify();
      _connect();
    }
    _resync();
  }

  Future<void> saveSettings(
    bool thinkingEnabled,
    String effort,
    int maxContext,
    int maxOutput,
  ) async {
    try {
      await api.setSettings({
        'thinking_enabled': thinkingEnabled,
        'thinking_effort': effort,
        'max_context_tokens': maxContext,
        'max_output_tokens': maxOutput,
      });
      _resync();
    } catch (e) {
      showToast('не сохранилось: $e');
      rethrow;
    }
  }

  Future<void> compact(int keep) async {
    try {
      compacting = true;
      notify();
      final j = await api.compact(keep);
      compacting = false;
      showToast('compact: ${j['tokensBefore']} → ${j['tokensAfter']}');
      _resync();
    } catch (e) {
      compacting = false;
      showToast('compact: $e');
      notify();
    }
  }
}
