import 'package:flutter/material.dart';

import '../model.dart';
import '../state.dart';
import 'md.dart';
import 'toolfmt.dart';

class ChatActions extends StatelessWidget {
  final AgentApp app;
  const ChatActions({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    final st = app.status;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (st != null)
          Padding(
            padding: const EdgeInsets.only(right: 4),
            child: StatusChip(app: app, st: st),
          ),
        if (st != null && st.state == RunState.running)
          IconButton(
            icon: const Icon(Icons.stop),
            tooltip: 'стоп',
            onPressed: () => app.stop(),
          ),
        if (st != null && st.state == RunState.stopped)
          IconButton(
            icon: const Icon(Icons.play_arrow),
            tooltip: 'дальше',
            onPressed: () => app.resume(),
          ),
      ],
    );
  }
}

class StatusChip extends StatelessWidget {
  final AgentApp app;
  final HarnessStatus st;
  const StatusChip({super.key, required this.app, required this.st});

  @override
  Widget build(BuildContext context) {
    final (icon, color, label) = switch (st.state) {
      RunState.running => (Icons.play_circle, Colors.green, 'run ${st.turn}'),
      RunState.stopped => (Icons.pause_circle, Colors.orange, 'stopped'),
      RunState.done => (Icons.check_circle, Colors.blueGrey, 'done'),
      RunState.error => (Icons.error, Colors.red, 'error'),
      RunState.idle => (Icons.remove_circle, Colors.blueGrey, 'idle'),
    };
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, color: color, size: 18),
        const SizedBox(width: 4),
        Text(label, style: Theme.of(context).textTheme.labelMedium),
        const SizedBox(width: 8),
        if (st.maxContextTokens > 0)
          Text(
            'ctx ${st.promptTokensLast} / ${st.maxContextTokens}',
            style: Theme.of(context).textTheme.labelSmall,
          ),
        if (st.maxContextTokens > 0 && st.activeSubagentCount > 0)
          const SizedBox(width: 8),
        if (st.activeSubagentCount > 0)
          Text(
            'агенты ${st.activeSubagentCount}',
            style: Theme.of(context).textTheme.labelSmall,
          ),
      ],
    );
  }
}

class ChatBanner extends StatelessWidget {
  final AgentApp app;
  const ChatBanner({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    final Widget? banner = switch (app.conn) {
      ConnState.badToken => const _Bar(
        color: Colors.red,
        icon: Icons.lock,
        text: 'токен не подходит — соберите APK заново',
      ),
      ConnState.down => const _Bar(
        color: Colors.orange,
        icon: Icons.cloud_off,
        text: 'сервер не доступен, переподключаюсь…',
      ),
      ConnState.connecting =>
        app.harnessDown
            ? const _Bar(
                color: Colors.deepOrange,
                icon: Icons.laptop,
                text: 'агент на Mac офлайн',
              )
            : null,
      ConnState.ok =>
        app.harnessDown
            ? const _Bar(
                color: Colors.deepOrange,
                icon: Icons.laptop,
                text: 'агент на Mac офлайн',
              )
            : null,
    };
    return banner ?? const SizedBox.shrink();
  }
}

class _Bar extends StatelessWidget {
  final Color color;
  final IconData icon;
  final String text;
  const _Bar({required this.color, required this.icon, required this.text});

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      color: color,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: Row(
        children: [
          Icon(icon, size: 16, color: Colors.white),
          const SizedBox(width: 8),
          Text(text, style: const TextStyle(color: Colors.white)),
        ],
      ),
    );
  }
}

class MessageList extends StatefulWidget {
  final AgentApp app;
  const MessageList({super.key, required this.app});
  @override
  State<MessageList> createState() => MessageListState();
}

class MessageListState extends State<MessageList> {
  final ScrollController _sc = ScrollController();
  int _lastCount = -1;
  int? _lastLiveLen;

  @override
  void dispose() {
    _sc.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final app = widget.app;
    final items = [...app.messages, if (app.live != null) app.live!];
    final changed =
        items.length != _lastCount ||
        (app.live?.text.length ?? -1) != (_lastLiveLen ?? -1);
    _lastCount = items.length;
    _lastLiveLen = app.live?.text.length;
    if (changed && _sc.hasClients) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && _sc.hasClients) {
          _sc.jumpTo(_sc.position.maxScrollExtent);
        }
      });
    }
    return ListView.builder(
      controller: _sc,
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
      itemCount: items.length,
      itemBuilder: (context, i) => MessageTile(
        msg: items[i],
        live: i == items.length - 1 && identical(items[i], app.live),
        onCopy: () => app.showToast('скопировано'),
      ),
    );
  }
}

class MessageTile extends StatelessWidget {
  final ViewMsg msg;
  final bool live;
  final VoidCallback onCopy;
  const MessageTile({
    super.key,
    required this.msg,
    required this.live,
    required this.onCopy,
  });

  static const double fontSize = 15;

  @override
  Widget build(BuildContext context) {
    final dark = Theme.of(context).brightness == Brightness.dark;
    final muted = dark ? Colors.grey.shade400 : Colors.grey.shade500;
    final link = dark ? Colors.blue.shade300 : Colors.blue.shade800;
    if (msg.role == 'user') {
      return Padding(
        padding: const EdgeInsets.only(bottom: 14),
        child: MdView(
          text: msg.text,
          body: TextStyle(
            fontSize: fontSize,
            height: 1.35,
            fontWeight: FontWeight.w600,
            color: dark ? Colors.green.shade300 : Colors.green.shade800,
          ),
          link: link,
          onCopy: onCopy,
        ),
      );
    }
    final empty = msg.parts.isEmpty && msg.error == null;
    final blocks = <Widget>[];
    final pendingTools = <ToolCard>[];
    void flushTools() {
      if (pendingTools.isEmpty) return;
      blocks.add(
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            for (final t in pendingTools)
              _ToolBlock(
                t,
                muted: muted,
                dark: dark,
                key: ValueKey(t.id),
              ),
          ],
        ),
      );
      pendingTools.clear();
    }

    for (var i = 0; i < msg.parts.length; i++) {
      final p = msg.parts[i];
      if (p is ToolCard) {
        pendingTools.add(p);
        continue;
      }
      final part = p as Part;
      flushTools();
      if (part.content.isEmpty) continue;
      if (part.kind == PartKind.thinking) {
        blocks.add(
          _ThinkingBlock(
            key: ValueKey('think-$i'),
            text: part.content,
            body: TextStyle(fontSize: fontSize, height: 1.35, color: muted),
            link: link,
            onCopy: onCopy,
          ),
        );
        continue;
      }
      blocks.add(
        MdView(
          text: part.content,
          body: TextStyle(
            fontSize: fontSize,
            height: 1.35,
            color: dark ? Colors.grey.shade200 : Colors.grey.shade800,
          ),
          link: link,
          onCopy: onCopy,
        ),
      );
    }
    flushTools();
    return Padding(
      padding: const EdgeInsets.only(bottom: 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ...blocks,
          if (msg.usage != null)
            Text(
              msg.usage!.describe(),
              style: TextStyle(fontSize: 11, color: muted),
            ),
          if (msg.error != null)
            Text(
              msg.error!,
              style: const TextStyle(color: Colors.red, fontSize: 12),
            ),
          if (live && empty)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Row(
                children: [
                  const SizedBox(
                    width: 14,
                    height: 14,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  ),
                  const SizedBox(width: 8),
                  Text('думает…', style: TextStyle(fontSize: 12, color: muted)),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

String _flat(String s) => s.trim().replaceAll(RegExp(r'\s+'), ' ');

class _ToggleRow extends StatelessWidget {
  final String text;
  final TextStyle style;
  final VoidCallback onTap;
  const _ToggleRow({
    required this.text,
    required this.style,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Text(
          text,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: style,
        ),
      ),
    );
  }
}

class _ToolBlock extends StatefulWidget {
  final ToolCard t;
  final Color muted;
  final bool dark;
  const _ToolBlock(
    this.t, {
    required this.muted,
    required this.dark,
    super.key,
  });

  @override
  State<_ToolBlock> createState() => _ToolBlockState();
}

class _ToolBlockState extends State<_ToolBlock> {
  bool _open = false;

  @override
  Widget build(BuildContext context) {
    final t = widget.t;
    final mark = t.ok == null ? '…' : (t.ok == true ? '✓' : '✗');
    final sum = toolSummary(t.name, t.argsPreview);
    final head = sum.isEmpty ? '$mark ${t.name}' : '$mark ${t.name} — $sum';
    final small = TextStyle(
      fontSize: 13,
      height: 1.3,
      fontFamily: 'monospace',
      color: widget.muted,
    );
    final preStyle = TextStyle(
      fontSize: 12,
      height: 1.35,
      fontFamily: 'monospace',
      color: widget.dark ? Colors.grey.shade200 : Colors.grey.shade800,
    );
    final result = (t.resultPreview ?? '').trim();
    final args = prettyArgs(t.argsPreview);
    Widget label(String s) => Padding(
          padding: const EdgeInsets.fromLTRB(8, 6, 8, 2),
          child: Text(
            s,
            style: TextStyle(
              fontSize: 10,
              letterSpacing: 0.6,
              color: widget.muted,
            ),
          ),
        );
    Widget pre(String s) => Padding(
          padding: const EdgeInsets.fromLTRB(8, 0, 8, 6),
          child: SelectableText(s, style: preStyle),
        );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _ToggleRow(
          text: head,
          style: small,
          onTap: () => setState(() => _open = !_open),
        ),
        if (_open) ...[
          if (result.isNotEmpty)
            Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [label('результат'), pre(result)],
            ),
          if (args != null && args.isNotEmpty)
            Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [label('аргументы'), pre(args)],
            ),
        ],
      ],
    );
  }
}

class _ThinkingBlock extends StatefulWidget {
  final String text;
  final TextStyle body;
  final Color link;
  final VoidCallback onCopy;
  const _ThinkingBlock({
    required this.text,
    required this.body,
    required this.link,
    required this.onCopy,
    super.key,
  });

  @override
  State<_ThinkingBlock> createState() => _ThinkingBlockState();
}

class _ThinkingBlockState extends State<_ThinkingBlock> {
  bool _open = false;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _ToggleRow(
          text: _flat(widget.text),
          style: widget.body,
          onTap: () => setState(() => _open = !_open),
        ),
        if (_open)
          Padding(
            padding: const EdgeInsets.only(bottom: 4),
            child: MdView(
              text: widget.text,
              body: widget.body,
              link: widget.link,
              onCopy: widget.onCopy,
            ),
          ),
      ],
    );
  }
}

class InputBar extends StatefulWidget {
  final AgentApp app;
  const InputBar({super.key, required this.app});
  @override
  State<InputBar> createState() => InputBarState();
}

class InputBarState extends State<InputBar> {
  final TextEditingController _text = TextEditingController();
  final FocusNode _focus = FocusNode();

  @override
  void initState() {
    super.initState();
    _text.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _text.dispose();
    _focus.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(12, 6, 12, 10),
      decoration: BoxDecoration(
        border: Border(
          top: BorderSide(color: Colors.grey.withValues(alpha: 0.3)),
        ),
      ),
      child: Row(
        children: [
          Expanded(
            child: TextField(
              controller: _text,
              focusNode: _focus,
              minLines: 1,
              maxLines: 5,
              textInputAction: TextInputAction.send,
              decoration: const InputDecoration(
                hintText: 'сообщение (в работе — steering)',
                border: OutlineInputBorder(),
                isDense: true,
              ),
              onSubmitted: (v) => _send(),
            ),
          ),
          const SizedBox(width: 8),
          IconButton.filled(
            icon: const Icon(Icons.send),
            onPressed: _text.text.trim().isEmpty ? null : _send,
          ),
        ],
      ),
    );
  }

  void _send() {
    final v = _text.text;
    if (v.trim().isEmpty) return;
    _text.clear();
    FocusScope.of(context).unfocus();
    widget.app.send(v);
  }
}