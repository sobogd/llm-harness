import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:markdown/markdown.dart' as md;

final md.Document _doc = md.Document(
  extensionSet: md.ExtensionSet.gitHubFlavored,
);

class MdSpan extends TextSpan {
  final String? copy;
  const MdSpan({
    this.copy,
    super.text,
    super.children,
    super.style,
    super.recognizer,
    super.semanticsLabel,
  });
}

int _len(TextSpan s) {
  var n = s.text?.length ?? 0;
  for (final c in s.children ?? const <InlineSpan>[]) {
    n += _len(c as TextSpan);
  }
  return n;
}

final RegExp _url = RegExp(r"""(?:https?://|www\.)[^\s<>"']{2,}""");

const _trailingPunct = ',.;:!?"\'';

TextStyle _linkStyle(TextStyle base, Color c) => base.copyWith(
      color: c,
      decoration: TextDecoration.underline,
      decorationColor: c,
    );

TextStyle _codeStyle(TextStyle base) => base.copyWith(
      fontFamily: 'monospace',
      fontSize: (base.fontSize ?? 14) - 1,
    );

List<InlineSpan> _linkify(
  String s,
  TextStyle base,
  Color link, {
  bool plain = false,
}) {
  if (s.isEmpty) return const [];
  if (plain) return [TextSpan(text: s, style: base)];
  final out = <InlineSpan>[];
  var i = 0;
  for (final m in _url.allMatches(s)) {
    var e = m.end;
    while (e > m.start && _trailingPunct.contains(s[e - 1])) {
      e--;
    }
    if (e <= m.start) continue;
    if (m.start > i) {
      out.add(TextSpan(text: s.substring(i, m.start), style: base));
    }
    final u = s.substring(m.start, e);
    out.add(MdSpan(text: u, copy: u, style: _linkStyle(base, link)));
    i = e;
  }
  if (i < s.length) {
    out.add(TextSpan(text: s.substring(i), style: base));
  }
  return out;
}

TextSpan buildInline(
  List<md.Node>? kids,
  TextStyle base,
  Color link, {
  bool plain = false,
}) {
  final list = <InlineSpan>[];
  for (final k in kids ?? const <md.Node>[]) {
    if (k is md.Text) {
      list.addAll(_linkify(k.text, base, link, plain: plain));
    } else if (k is md.Element) {
      list.add(_el(k, base, link, plain: plain));
    }
  }
  return TextSpan(children: list);
}

InlineSpan _el(
  md.Element el,
  TextStyle base,
  Color link, {
  bool plain = false,
}) {
  final kids = el.children;
  switch (el.tag) {
    case 'a':
      final url = el.attributes['href'] ?? '';
      final ls = _linkStyle(base, link);
      return MdSpan(
        copy: url,
        style: ls,
        children: [buildInline(kids, ls, link, plain: true)],
      );
    case 'strong':
      final b = base.copyWith(fontWeight: FontWeight.w700);
      return MdSpan(
        copy: el.textContent,
        style: b,
        children: [buildInline(kids, b, link, plain: plain)],
      );
    case 'em':
      final it = base.copyWith(fontStyle: FontStyle.italic);
      return TextSpan(
        style: it,
        children: [buildInline(kids, it, link, plain: plain)],
      );
    case 'del':
      final d = base.copyWith(decoration: TextDecoration.lineThrough);
      return TextSpan(
        style: d,
        children: [buildInline(kids, d, link, plain: plain)],
      );
    case 'code':
      return TextSpan(text: el.textContent, style: _codeStyle(base));
    case 'br':
      return TextSpan(text: '\n', style: base);
    case 'img':
      final src = el.attributes['src'] ?? '';
      final alt = el.attributes['alt'] ?? '';
      return MdSpan(
        copy: src,
        text: alt.isEmpty ? 'изображение' : alt,
        style: _linkStyle(base, link),
      );
    default:
      return buildInline(kids, base, link, plain: plain);
  }
}

String? _copyAt(TextSpan root, int offset) {
  String? best;
  var bestDepth = -1;
  void visit(TextSpan s, int start, int depth) {
    final end = start + _len(s);
    if (s is MdSpan &&
        s.copy != null &&
        depth > bestDepth &&
        offset >= start &&
        offset < end) {
      best = s.copy;
      bestDepth = depth;
    }
    var p = start + (s.text?.length ?? 0);
    for (final c in s.children ?? const <InlineSpan>[]) {
      visit(c as TextSpan, p, depth + 1);
      p += _len(c);
    }
  }

  visit(root, 0, 0);
  return best;
}

class MdPara extends StatefulWidget {
  final TextSpan span;
  final VoidCallback? onCopy;
  const MdPara({super.key, required this.span, this.onCopy});
  @override
  State<MdPara> createState() => _MdParaState();
}

class _MdParaState extends State<MdPara> {
  bool _selecting = false;

  void _tap(TapDownDetails d, BuildContext context) {
    final size = context.size;
    if (size == null) return;
    final tp = TextPainter(
      text: widget.span,
      textDirection: TextDirection.ltr,
      textScaler: MediaQuery.textScalerOf(context),
    )..layout(maxWidth: size.width);
    final p = Offset(
      d.localPosition.dx.clamp(0.0, tp.width),
      d.localPosition.dy.clamp(0.0, tp.height),
    );
    final hit = _copyAt(widget.span, tp.getPositionForOffset(p).offset);
    if (hit != null) {
      Clipboard.setData(ClipboardData(text: hit));
      widget.onCopy?.call();
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_len(widget.span) == 0) return const SizedBox.shrink();
    if (!_selecting) {
      return GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTapDown: (d) => _tap(d, context),
        onLongPress: () => setState(() => _selecting = true),
        child: Text.rich(
          widget.span,
          textScaler: MediaQuery.textScalerOf(context),
        ),
      );
    }
    return Stack(
      clipBehavior: Clip.none,
      children: [
        SelectableText.rich(
          widget.span,
          textScaler: MediaQuery.textScalerOf(context),
        ),
        Positioned(
          top: -8,
          right: -4,
          child: InkWell(
            onTap: () => setState(() => _selecting = false),
            child: CircleAvatar(
              radius: 10,
              backgroundColor:
                  Theme.of(context).colorScheme.surfaceContainerHighest,
              child: const Icon(Icons.close, size: 12),
            ),
          ),
        ),
      ],
    );
  }
}

class _Renderer {
  final BuildContext context;
  final TextStyle body;
  final Color link;
  final VoidCallback? onCopy;
  _Renderer(this.context, this.body, this.link, this.onCopy);

  Widget block(md.Node n) {
    if (n is md.Text) {
      return MdPara(
        span: TextSpan(text: n.text, style: body),
        onCopy: onCopy,
      );
    }
    final el = n as md.Element;
    switch (el.tag) {
      case 'h1':
      case 'h2':
      case 'h3':
      case 'h4':
      case 'h5':
      case 'h6':
        final sz = {'h1': 18.0, 'h2': 17.0, 'h3': 16.0}[el.tag] ?? 15.0;
        return Padding(
          padding: const EdgeInsets.only(top: 8, bottom: 2),
          child: Text.rich(
            buildInline(
              el.children,
              body.copyWith(fontSize: sz, fontWeight: FontWeight.w700),
              link,
            ),
          ),
        );
      case 'pre':
        return _CodeText(el.textContent, lang: _lang(el), onCopy: onCopy);
      case 'blockquote':
        return Container(
          margin: const EdgeInsets.symmetric(vertical: 4),
          padding: const EdgeInsets.only(left: 10),
          decoration: BoxDecoration(
            border: Border(
              left: BorderSide(
                color: Theme.of(context).dividerColor,
                width: 3,
              ),
            ),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [for (final c in el.children ?? const []) block(c)],
          ),
        );
      case 'ul':
      case 'ol':
        return _List(el: el, ordered: el.tag == 'ol', r: this);
      case 'hr':
        return const Divider(height: 12, indent: 8, endIndent: 8);
      case 'table':
        final lines = el.children
            ?.whereType<md.Element>()
            .where((e) => e.tag == 'tr')
            .map((tr) => tr.children
                ?.whereType<md.Element>()
                .map((td) => td.textContent.trim())
                .join(' | '))
            .toList();
        return _CodeText(
          (lines ?? const []).join('\n'),
          lang: 'таблица',
          onCopy: onCopy,
        );
      default:
        return MdPara(
          span: buildInline(el.children, body, link),
          onCopy: onCopy,
        );
    }
  }

  String _lang(md.Element pre) {
    final code = pre.children
        ?.whereType<md.Element>()
        .where((e) => e.tag == 'code')
        .firstOrNull;
    final cls = code?.attributes['class'] ?? '';
    return cls.startsWith('language-') ? cls.substring(9) : '';
  }
}

class MdView extends StatelessWidget {
  final String text;
  final TextStyle body;
  final Color link;
  final VoidCallback? onCopy;
  const MdView({
    super.key,
    required this.text,
    required this.body,
    required this.link,
    this.onCopy,
  });

  @override
  Widget build(BuildContext context) {
    if (text.trim().isEmpty) return const SizedBox.shrink();
    final r = _Renderer(context, body, link, onCopy);
    final nodes = _doc.parse(text);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [for (final n in nodes) r.block(n)],
    );
  }
}

class _List extends StatelessWidget {
  final md.Element el;
  final bool ordered;
  final _Renderer r;
  const _List({required this.el, required this.ordered, required this.r});

  @override
  Widget build(BuildContext context) {
    final items = el.children
        ?.whereType<md.Element>()
        .where((e) => e.tag == 'li')
        .toList() ??
        const <md.Element>[];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [for (var i = 0; i < items.length; i++) _item(items[i], i)],
    );
  }

  Widget _item(md.Element li, int idx) {
    final inline = <md.Node>[];
    final blocks = <Widget>[];
    for (final c in li.children ?? const <md.Node>[]) {
      if (c is md.Element &&
          (c.tag == 'ul' || c.tag == 'ol' || c.tag == 'pre' || c.tag == 'blockquote')) {
        blocks.add(r.block(c));
      } else {
        inline.add(c);
      }
    }
    return Padding(
      padding: const EdgeInsets.only(top: 2),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(ordered ? '${idx + 1}.' : '•', style: r.body),
          const SizedBox(width: 6),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                MdPara(
                  span: buildInline(inline, r.body, r.link),
                  onCopy: r.onCopy,
                ),
                ...blocks,
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _CodeText extends StatelessWidget {
  final String code;
  final String lang;
  final VoidCallback? onCopy;
  const _CodeText(this.code, {required this.lang, this.onCopy});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 5),
      padding: const EdgeInsets.fromLTRB(10, 4, 10, 8),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(6),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              Text(
                lang.isEmpty ? 'код' : lang,
                style: TextStyle(
                  fontSize: 11,
                  fontFamily: 'monospace',
                  color: cs.onSurfaceVariant,
                ),
              ),
              const Spacer(),
              InkWell(
                onTap: () {
                  Clipboard.setData(ClipboardData(text: code));
                  onCopy?.call();
                },
                child: Padding(
                  padding: const EdgeInsets.all(2),
                  child: Icon(
                    Icons.copy,
                    size: 16,
                    color: cs.onSurfaceVariant,
                  ),
                ),
              ),
            ],
          ),
          SelectableText(
            code,
            style: const TextStyle(
              fontSize: 13,
              height: 1.3,
              fontFamily: 'monospace',
            ),
          ),
        ],
      ),
    );
  }
}