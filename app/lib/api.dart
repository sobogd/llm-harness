import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'config.dart';

class AuthException implements Exception {
  final String message;
  AuthException(this.message);
  @override
  String toString() => 'AuthException: $message';
}

class ApiError implements Exception {
  final int code;
  final String body;
  ApiError(this.code, this.body);
  @override
  String toString() => 'ApiError $code: $body';
}

class SseEvent {
  final int? id;
  final String event;
  final Map<String, dynamic> data;
  SseEvent({this.id, required this.event, required this.data});
}

class HarnessApi {
  String token = authToken;
  final http.Client _client = http.Client();

  void dispose() => _client.close();

  Map<String, String> get _headers => {
        'Authorization': 'Bearer $token',
        'Content-Type': 'application/json',
      };

  Future<Map<String, dynamic>> health() async {
    final r = await _client.get(Uri.parse('$serverUrl/health'));
    if (r.statusCode != 200) throw ApiError(r.statusCode, r.body);
    return (jsonDecode(r.body) as Map).cast<String, dynamic>();
  }

  Future<Map<String, dynamic>> call(
      String path, Map<String, dynamic> body) async {
    final r = await _client.post(Uri.parse('$serverUrl$path'),
        headers: _headers, body: jsonEncode(body));
    if (r.statusCode == 401) throw AuthException('bad token (APK older than server?)');
    if (r.statusCode >= 400) throw ApiError(r.statusCode, r.body);
    return (jsonDecode(r.body) as Map).cast<String, dynamic>();
  }

  Future<Map<String, dynamic>> status() => call('/status', const {});

  Future<Map<String, dynamic>> ask(String prompt) =>
      call('/ask', {'prompt': prompt});

  Future<Map<String, dynamic>> stop() => call('/stop', const {});

  Future<Map<String, dynamic>> resume() => call('/resume', const {});

  Future<Map<String, dynamic>> setSettings(Map<String, dynamic> body) =>
      call('/settings', body);

  Future<Map<String, dynamic>> compact(int keep) =>
      call('/compact', {'keep_last_messages': keep});

  Future<Map<String, dynamic>> newSession() =>
      call('/new-session', const {});

  Future<Map<String, dynamic>> listSessions() =>
      call('/list-sessions', const {});

  Future<Map<String, dynamic>> loadSession(String id) =>
      call('/load-session', {'session_id': id});

  Future<Map<String, dynamic>> deleteSession(String id) =>
      call('/delete-session', {'session_id': id});

  Future<List<Map<String, dynamic>>> messages({int last = 0}) async {
    final j = await call('/get-messages', {'last': last});
    return (j['m'] as List? ?? const [])
        .map((e) => (e as Map).cast<String, dynamic>())
        .toList();
  }

  Stream<SseEvent> events({required int since}) async* {
    final req = http.Request('GET', Uri.parse('$serverUrl/events?since=$since'))
      ..headers.addAll(_headers);
    final stream = await _client.send(req);
    if (stream.statusCode == 401) throw AuthException('bad token');
    if (stream.statusCode != 200) throw ApiError(stream.statusCode, 'events');
    final lines = const LineSplitter().bind(utf8.decoder.bind(stream.stream));
    var id = -1;
    var event = 'message';
    var sawComment = false;
    final data = <String>[];
    await for (final line in lines) {
      if (line.isEmpty) {
        if (data.isNotEmpty) {
          final raw = data.join('\n');
          Map<String, dynamic> d;
          try {
            d = (jsonDecode(raw) as Map).cast<String, dynamic>();
          } catch (_) {
            d = const {};
          }
          yield SseEvent(
            id: id >= 0 ? id : d['seq'] as int?,
            event: event,
            data: d,
          );
        } else if (sawComment) {
          yield SseEvent(id: null, event: 'keepalive', data: const {});
        }
        id = -1;
        event = 'message';
        data.clear();
        sawComment = false;
        continue;
      }
      if (line.startsWith(':')) {
        sawComment = true;
        continue;
      }
      final i = line.indexOf(':');
      final key = i < 0 ? line : line.substring(0, i);
      var value = i < 0 ? '' : line.substring(i + 1);
      if (value.startsWith(' ')) value = value.substring(1);
      switch (key) {
        case 'id':
          id = int.tryParse(value) ?? -1;
        case 'event':
          event = value;
        case 'data':
          data.add(value);
      }
    }
  }
}
