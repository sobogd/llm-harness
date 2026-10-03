import 'package:flutter/material.dart';

import '../config.dart';
import '../state.dart';
import '../updater.dart';

const _efforts = ['minimal', 'low', 'medium', 'xhigh'];

class SettingsScreen extends StatefulWidget {
  final AgentApp app;
  const SettingsScreen({super.key, required this.app});
  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late bool _thinking;
  late String _effort;
  late TextEditingController _maxCtx;
  late TextEditingController _maxOut;
  late TextEditingController _token;
  bool _saving = false;

  late final Updater _updater;

  @override
  void initState() {
    super.initState();
    final s = widget.app.status?.settings ?? const {};
    _thinking = (s['thinking_enabled'] ?? true) as bool;
    _effort = _efforts.contains(s['thinking_effort'])
        ? s['thinking_effort'] as String
        : 'medium';
    _token = TextEditingController(text: widget.app.savedToken);
    _maxCtx = TextEditingController(
        text: '${(s['max_context_tokens'] ?? 60000) as int}');
    _maxOut = TextEditingController(
        text: '${(s['max_output_tokens'] ?? 8192) as int}');

    _updater = Updater();
    // При открытии настроек сразу смотрим, есть ли новая версия.
    if (_updater.enabled) _updater.checkNow();
  }

  @override
  void dispose() {
    _updater.dispose();
    _maxCtx.dispose();
    _maxOut.dispose();
    _token.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('настройки')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          TextField(
            controller: _token,
            decoration: const InputDecoration(
                isDense: true, hintText: 'токен сервера'),
          ),
          const SizedBox(height: 16),
          SwitchListTile(
            title: const Text('thinking'),
            value: _thinking,
            onChanged: (v) => setState(() => _thinking = v),
          ),
          const SizedBox(height: 8),
          const Text('усилие thinking'),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            children: _efforts.map((e) => ChoiceChip(
                  label: Text(e),
                  selected: _effort == e,
                  onSelected: (sel) {
                    if (sel) setState(() => _effort = e);
                  },
                )).toList(),
          ),
          const SizedBox(height: 16),
          Row(children: [
            const Expanded(child: Text('контекст (токены)')),
            const Expanded(child: Text('ответ (токены)')),
          ]),
          Row(children: [
            Expanded(
              child: TextField(
                controller: _maxCtx,
                keyboardType: TextInputType.number,
                decoration: const InputDecoration(isDense: true),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: TextField(
                controller: _maxOut,
                keyboardType: TextInputType.number,
                decoration: const InputDecoration(isDense: true),
              ),
            ),
          ]),
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _saving ? null : _save,
            child: _saving
                ? const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('сохранить'),
          ),
          const SizedBox(height: 24),
          const Text('компрессия истории'),
          const SizedBox(height: 8),
          OutlinedButton(
            onPressed: _saving
                ? null
                : () =>
                    widget.app.compact(10),
            child: const Text('сжать, оставив последние 10 сообщений'),
          ),
          const Divider(height: 32),
          _updateSection(),
        ],
      ),
    );
  }

  Widget _updateSection() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text('обновления',
            style: TextStyle(fontWeight: FontWeight.w600)),
        const SizedBox(height: 4),
        Text('версия $appVersion',
            style: Theme.of(context).textTheme.bodySmall),
        const SizedBox(height: 8),
        ListenableBuilder(
          listenable: _updater,
          builder: (context, _) => _updateBody(_updater),
        ),
      ],
    );
  }

  Widget _updateBody(Updater u) {
    switch (u.status) {
      case UpdateStatus.disabled:
        return const Text(
          'проверка обновлений недоступна в этой сборке',
          style: TextStyle(color: Colors.grey),
        );
      case UpdateStatus.idle:
        return _checkButton(u);
      case UpdateStatus.checking:
        return const Row(children: [
          SizedBox(
              width: 16,
              height: 16,
              child: CircularProgressIndicator(strokeWidth: 2)),
          SizedBox(width: 12),
          Text('проверяем…'),
        ]);
      case UpdateStatus.none:
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('публикаций пока нет'),
            const SizedBox(height: 8),
            _checkButton(u),
          ],
        );
      case UpdateStatus.upToDate:
        return const Text('установлена последняя версия ✓');
      case UpdateStatus.available:
        final info = u.latest!;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('доступна версия ${info.version}'),
            if (info.size > 0)
              Text('${(info.size / 1048576).toStringAsFixed(1)} МБ',
                  style: Theme.of(context).textTheme.bodySmall),
            if (u.error != null)
              Text(u.error!,
                  style: const TextStyle(color: Colors.orange)),
            const SizedBox(height: 8),
            FilledButton(
              onPressed: u.installNow,
              child: const Text('скачать и установить'),
            ),
            const SizedBox(height: 4),
            TextButton(
              onPressed: u.checkNow,
              child: const Text('проверить ещё раз'),
            ),
          ],
        );
      case UpdateStatus.downloading:
        final pct = (u.progress * 100).round();
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            LinearProgressIndicator(value: u.progress),
            const SizedBox(height: 6),
            Text('скачивание… $pct%'),
          ],
        );
      case UpdateStatus.ready:
        return const Text('установщик открыт — подтвердите установку');
      case UpdateStatus.error:
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(u.error ?? 'ошибка',
                style: const TextStyle(color: Colors.red)),
            const SizedBox(height: 8),
            _checkButton(u),
          ],
        );
    }
  }

  Widget _checkButton(Updater u) => FilledButton(
        onPressed: u.checkNow,
        child: const Text('проверить обновление'),
      );

  Future<void> _save() async {
    final ctx = int.tryParse(_maxCtx.text.trim());
    final out = int.tryParse(_maxOut.text.trim());
    if (ctx == null || ctx <= 0 || out == null || out <= 0) {
      widget.app.showToast('введите числовые значения');
      return;
    }
    if (ctx + out > 204800 - 2048) {
      widget.app.showToast('контекст + ответ больше лимита сервера (202752)');
      return;
    }
    setState(() => _saving = true);
    try {
      await widget.app.saveToken(_token.text);
      await widget.app.saveSettings(_thinking, _effort, ctx, out);
      if (mounted) Navigator.pop(context);
    } catch (_) {} finally {
      if (mounted) setState(() => _saving = false);
    }
  }
}