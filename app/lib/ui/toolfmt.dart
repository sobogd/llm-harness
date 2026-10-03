import 'dart:convert';

/// LLM иногда проносит HTML-сущности прямо в аргументах/выводе
/// (" и т.п.) — декодируем перед парсингом и выводом.
String decodeEntities(String s) {
  if (!s.contains('&')) return s;
  return s
      .replaceAll('&' 'quot;', '"')
      .replaceAll('&' 'apos;', "'")
      .replaceAll('&' 'lt;', '<')
      .replaceAll('&' 'gt;', '>')
      .replaceAll('&' 'nbsp;', ' ')
      .replaceAll('&' 'amp;', '&');
}

String flat(String s) => s.trim().replaceAll(RegExp(r'\s+'), ' ');

/// Аргументы инструмента LLM в виде карты. null, если не распарсилось
/// (например, живое превью — обрезанное посреди потока JSON).
Map<String, dynamic>? parseArgs(String? raw) {
  if (raw == null) return null;
  final s = decodeEntities(raw).trim();
  if (s.isEmpty || !s.startsWith('{')) return null;
  try {
    final v = jsonDecode(s);
    if (v is Map) return v.map((k, val) => MapEntry(k.toString(), val));
  } catch (_) {
    // обрезанный JSON: берём первое попавшее строковое поле
  }
  // значение может не быть закрытым (превью режется посреди потока)
  final m = RegExp(r'"([a-zA-Z0-9_]+)"\s*:\s*"((?:[^"\\]|\\.)*)')
      .firstMatch(s);
  if (m == null) return null;
  final val = m.group(2)!
      .replaceAll(r'\"', '"')
      .replaceAll(r'\n', '\n')
      .replaceAll(r'\\', r'\');
  return {m.group(1)!: val};
}

/// Что показывать в свернутой строке: для bash — сама команда,
/// для файловых — путь, для поиска — запрос.
String toolSummary(String name, String? rawArgs) {
  final a = parseArgs(rawArgs);
  String pick(String k) {
    final v = a?[k];
    return v == null ? '' : flat('$v');
  }
  String fallback() => rawArgs == null ? '' : flat(decodeEntities(rawArgs));
  switch (name) {
    case 'bash':
      var s = pick('command');
      if (s.isEmpty) s = fallback();
      if (s.isEmpty) return '';
      final t = a?['timeout'];
      if (t != null && '$t' != '120') s += ' · ${t}s';
      return s;
    case 'read':
      final p = pick('path');
      if (p.isEmpty) return '';
      final off = a?['offset'];
      final lim = a?['limit'];
      if (off != null || lim != null) {
        final o = off is int ? off : 1;
        final l = lim is int ? lim : 2000;
        return '$p :L$o–${o + l - 1}';
      }
      return p;
    case 'write':
      final p = pick('path');
      if (p.isEmpty) return '';
      final c = a?['content'];
      return c is String && c.isNotEmpty ? '$p · ${c.length} симв.' : p;
    case 'edit':
      return pick('path');
    case 'web_search':
      return pick('query');
    case 'browser_fetch':
      return pick('url');
    default:
      return fallback();
  }
}

/// Полный вывод аргументов для раскрытого вида: pretty JSON,
/// длинные строки (content write и т.п.) усекаются.
String? prettyArgs(String? raw) {
  if (raw == null || raw.trim().isEmpty) return null;
  final a = parseArgs(raw);
  if (a == null) return decodeEntities(raw);
  a.forEach((k, v) {
    if (v is String && v.length > 500) {
      a[k] = '${v.substring(0, 500)}… (+${v.length - 500} симв.)';
    }
  });
  return const JsonEncoder.withIndent('  ').convert(a);
}