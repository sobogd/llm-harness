import 'package:flutter/material.dart';

import '../model.dart';
import '../state.dart';

String _fmt(int ms) {
  final d = DateTime.fromMillisecondsSinceEpoch(ms).toLocal();
  final now = DateTime.now();
  String two(int v) => v.toString().padLeft(2, '0');
  final sameDay =
      d.year == now.year && d.month == now.month && d.day == now.day;
  return sameDay
      ? '${two(d.hour)}:${two(d.minute)}'
      : '${two(d.day)}.${two(d.month)}.${d.year}';
}

class SessionRow extends StatelessWidget {
  final SessionInfo s;
  final bool selected;
  final bool busy;
  final VoidCallback onTap;
  final VoidCallback onDelete;
  const SessionRow({
    super.key,
    required this.s,
    required this.selected,
    required this.busy,
    required this.onTap,
    required this.onDelete,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = selected
        ? theme.colorScheme.primary
        : theme.colorScheme.onSurface;
    return InkWell(
      onTap: busy && !selected ? null : onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Row(
          children: [
            Expanded(
              child: Text(
                s.preview,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: color,
                  fontWeight: selected ? FontWeight.w600 : null,
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.only(left: 8),
              child: Text(
                _fmt(s.updatedMs),
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                ),
              ),
            ),
            IconButton(
              icon: const Icon(Icons.delete_outline, size: 18),
              tooltip: 'удалить',
              onPressed: busy ? null : onDelete,
            ),
          ],
        ),
      ),
    );
  }
}

Future<void> confirmDelete(BuildContext context, AgentApp app, SessionInfo s) {
  return showDialog<void>(
    context: context,
    builder: (context) => AlertDialog(
      title: const Text('удалить сессию?'),
      content: Text(s.preview),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('отмена'),
        ),
        TextButton(
          onPressed: () {
            app.deleteSession(s.id);
            if (context.mounted) Navigator.of(context).pop();
          },
          child: const Text('удалить'),
        ),
      ],
    ),
  );
}

class SessionsListBody extends StatelessWidget {
  final AgentApp app;
  final ValueNotifier<String?> selected;
  final void Function(String id) onOpen;
  final void Function(SessionInfo s) onDeleted;
  const SessionsListBody({
    super.key,
    required this.app,
    required this.selected,
    required this.onOpen,
    required this.onDeleted,
  });

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<String?>(
      valueListenable: selected,
      builder: (context, selectedId, _) => ListenableBuilder(
        listenable: app,
        builder: (context, _) {
          if (app.sessions.isEmpty) {
            return Center(
              child: Text(
                app.sessionsError ? 'нет соединения' : 'пока пусто',
              ),
            );
          }
          return ListView.builder(
            itemCount: app.sessions.length,
            itemBuilder: (context, i) {
              final s = app.sessions[i];
              return SessionRow(
                s: s,
                selected: s.id == selectedId,
                busy: app.busy,
                onTap: () => onOpen(s.id),
                onDelete: () => confirmDelete(context, app, s),
              );
            },
          );
        },
      ),
    );
  }
}

class SessionsListScreen extends StatelessWidget {
  final AgentApp app;
  final ValueNotifier<String?> selected;
  final void Function(String id) onOpen;
  final void Function() onNew;
  final void Function() onSettings;
  final void Function(SessionInfo s) onDeleted;
  const SessionsListScreen({
    super.key,
    required this.app,
    required this.selected,
    required this.onOpen,
    required this.onNew,
    required this.onSettings,
    required this.onDeleted,
  });

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('harness'),
        centerTitle: false,
        actions: [
          IconButton(
            icon: const Icon(Icons.add),
            tooltip: 'новая сессия',
            onPressed: app.busy ? null : onNew,
          ),
          IconButton(
            icon: const Icon(Icons.settings),
            tooltip: 'настройки',
            onPressed: onSettings,
          ),
        ],
      ),
      body: SessionsListBody(
        app: app,
        selected: selected,
        onOpen: onOpen,
        onDeleted: onDeleted,
      ),
    );
  }
}