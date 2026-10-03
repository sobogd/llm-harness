enum RunState { idle, running, stopped, done, error }

RunState parseRunState(String? s) {
  switch (s) {
    case 'RUN_STATE_RUNNING':
      return RunState.running;
    case 'RUN_STATE_STOPPED':
      return RunState.stopped;
    case 'RUN_STATE_DONE':
      return RunState.done;
    case 'RUN_STATE_ERROR':
      return RunState.error;
    default:
      return RunState.idle;
  }
}

class Usage {
  final int prompt;
  final int completion;
  final int total;
  final int reasoning;
  final int cached;
  Usage({
    required this.prompt,
    required this.completion,
    required this.total,
    this.reasoning = 0,
    this.cached = 0,
  });

  factory Usage.fromJson(Map<String, dynamic> j) {
    final pd = (j['prompt_tokens_details'] ?? const {})
        as Map<String, dynamic>;
    final cd = (j['completion_tokens_details'] ?? const {})
        as Map<String, dynamic>;
    return Usage(
      prompt: (j['prompt_tokens'] ?? 0) as int,
      completion: (j['completion_tokens'] ?? 0) as int,
      total: (j['total_tokens'] ?? 0) as int,
      reasoning: (cd['reasoning_tokens'] ?? 0) as int,
      cached: (pd['cached_tokens'] ?? 0) as int,
    );
  }

  String describe() {
    final bits = [
      'prompt $prompt',
      'completion $completion',
      if (reasoning > 0) 'reasoning $reasoning',
      if (cached > 0) 'cached $cached',
    ];
    return bits.join(' · ');
  }
}

class ToolCard {
  final String id;
  final String name;
  final String? argsPreview;
  String? resultPreview;
  bool? ok;
  ToolCard({required this.id, required this.name, this.argsPreview});
}

enum PartKind { thinking, text }

class Part {
  final PartKind kind;
  String content;
  Part(this.kind, [this.content = '']);
}

class ViewMsg {
  final String role;
  final String? toolCallId;
  final List<Object> parts;
  Usage? usage;
  String? error;

  ViewMsg({
    required this.role,
    this.toolCallId,
    List<Object>? parts,
    this.usage,
    this.error,
  }) : parts = parts ?? [];

  factory ViewMsg.user(String text) =>
      ViewMsg(role: 'user', parts: [Part(PartKind.text, text)]);

  List<ToolCard> get tools => parts.whereType<ToolCard>().toList();

  String _join(PartKind kind) => parts
      .whereType<Part>()
      .where((p) => p.kind == kind)
      .map((p) => p.content)
      .join();

  String get text => _join(PartKind.text);

  String get thinking => _join(PartKind.thinking);

  void appendDelta(PartKind kind, String delta) {
    if (delta.isEmpty) return;
    final last = parts.isEmpty ? null : parts.last;
    if (last is Part && last.kind == kind) {
      last.content += delta;
    } else {
      parts.add(Part(kind, delta));
    }
  }

  void addTool(ToolCard card) => parts.add(card);

  ToolCard? toolById(String id) {
    for (final p in parts) {
      if (p is ToolCard && p.id == id) return p;
    }
    return null;
  }
}

class SessionInfo {
  final String id;
  final String preview;
  final int messages;
  final int createdMs;
  final int updatedMs;
  final bool active;
  SessionInfo({
    required this.id,
    required this.preview,
    required this.messages,
    required this.createdMs,
    required this.updatedMs,
    required this.active,
  });

  // proto int64 -> JSON string, int32 -> int: принимать оба
  static int _i(dynamic v) =>
      v is int ? v : int.tryParse('$v') ?? 0;

  factory SessionInfo.fromJson(Map<String, dynamic> j) => SessionInfo(
        id: (j['id'] ?? '') as String,
        preview: (j['preview'] ?? '') as String,
        messages: _i(j['messages']),
        createdMs: _i(j['created_ms']),
        updatedMs: _i(j['updated_ms']),
        active: (j['active'] ?? false) as bool,
      );

  String get updatedAt {
    if (updatedMs <= 0) return '';
    final t = DateTime.fromMillisecondsSinceEpoch(updatedMs);
    String two(int v) => v.toString().padLeft(2, '0');
    return '${two(t.day)}.${two(t.month)} ${two(t.hour)}:${two(t.minute)}';
  }
}

class HarnessStatus {
  final Map<String, dynamic> settings;
  final RunState state;
  final int turn;
  final String? lastError;
  final String sessionId;
  final int historyMessages;
  final int promptTokensLast;
  final Map<String, String> activeSubagents;
  HarnessStatus({
    required this.settings,
    required this.state,
    required this.turn,
    required this.lastError,
    required this.sessionId,
    required this.historyMessages,
    this.promptTokensLast = 0,
    this.activeSubagents = const {},
  });

  int get activeSubagentCount => activeSubagents.length;

  int get maxContextTokens => (settings['max_context_tokens'] ?? 0) as int;

  factory HarnessStatus.fromMap(Map<String, dynamic> j) {
    final s = (j['settings'] ?? const {}).cast<String, dynamic>();
    final run = (j['run'] ?? const {}).cast<String, dynamic>();
    return HarnessStatus(
      settings: s,
      state: parseRunState(run['state'] as String?),
      turn: (run['turn'] ?? 0) as int,
      lastError: run['last_error'] as String?,
      sessionId: (j['session_id'] ?? '') as String,
      historyMessages: (j['history_messages'] ?? 0) as int,
      promptTokensLast: (j['prompt_tokens_last'] ?? 0) as int,
      activeSubagents: _subs(j['active_subagents']),
    );
  }

  static Map<String, String> _subs(dynamic v) {
    if (v is Map) {
      return v.map((k, e) => MapEntry('$k', '$e'));
    }
    if (v is List) {
      final out = <String, String>{};
      for (final e in v) {
        if (e is! Map) continue;
        final id = e['id'];
        if (id == null) continue;
        out['$id'] = '${e['role'] ?? ''}'.trim();
      }
      return out;
    }
    return const {};
  }
}
