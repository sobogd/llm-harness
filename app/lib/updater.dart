import 'dart:async';
import 'dart:convert';
import 'dart:io' show File, HttpException;

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:open_filex/open_filex.dart';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'config.dart';

enum UpdateStatus {
  disabled,
  idle,
  checking,
  none,
  upToDate,
  available,
  downloading,
  ready,
  error,
}

/// Одна опубликованная сборка (мак отдаёт её в /update/manifest).
class LatestInfo {
  final int version;
  final String filename;
  final int size;
  final String sha256;

  LatestInfo({required this.version, required this.filename,
      required this.size, required this.sha256});

  factory LatestInfo.fromMap(Map<String, dynamic> m) => LatestInfo(
        version: int.tryParse('${m['version'] ?? 0}') ?? 0,
        filename: (m['filename'] ?? '') as String,
        size: int.tryParse('${m['size'] ?? 0}') ?? 0,
        sha256: (m['sha256'] ?? '') as String,
      );
}

/// Самобот: запрашивает /update/manifest, сравнивает версию-счётчик
/// (1, 2, 3, ...) с текущей, скачивает APK и открывает системный
/// установщик. Файл раздаёт мост (mac -> VPS -> телефон), S3 нет.
class Updater extends ChangeNotifier {
  final bool enabled = updateBaseUrl.isNotEmpty;
  UpdateStatus status =
      updateBaseUrl.isEmpty ? UpdateStatus.disabled : UpdateStatus.idle;
  double progress = 0;
  String? error;
  LatestInfo? latest;
  bool _checking = false;
  String? _downloadPath;

  /// Токен: введённый в настройках (SharedPreferences) либо вшитый при
  /// сборке. Обновления, как и всё API, идут под этим токеном.
  Future<String> _token() async {
    try {
      final p = await SharedPreferences.getInstance();
      final saved = p.getString('auth_token') ?? '';
      if (saved.isNotEmpty) return saved;
    } catch (_) {}
    return authToken;
  }

  void checkNow() {
    if (enabled) _check();
  }

  Future<void> _check() async {
    if (_checking) return;
    _checking = true;
    status = UpdateStatus.checking;
    error = null;
    progress = 0;
    notifyListeners();
    try {
      final client = http.Client();
      final r = await client
          .get(
            Uri.parse('$updateBaseUrl/update/manifest'),
            headers: {'Authorization': 'Bearer ${await _token()}'},
          )
          .timeout(const Duration(seconds: 20));
      client.close();
      if (r.statusCode != 200) {
        throw HttpException('manifest: HTTP ${r.statusCode}');
      }
      final m = json.decode(r.body) as Map<String, dynamic>;
      final info = LatestInfo.fromMap(m);
      latest = info;
      if (info.version == 0 || info.filename.isEmpty) {
        status = UpdateStatus.none;
      } else {
        final cur = int.tryParse(appVersion) ?? 0;
        status = info.version > cur
            ? UpdateStatus.available
            : UpdateStatus.upToDate;
      }
    } on Exception catch (e) {
      status = UpdateStatus.error;
      error = '$e';
    } finally {
      _checking = false;
      notifyListeners();
    }
  }

  Future<void> installNow() async {
    final info = latest;
    if (info == null || info.filename.isEmpty) {
      status = UpdateStatus.error;
      error = 'нет файла для установки';
      notifyListeners();
      return;
    }
    status = UpdateStatus.downloading;
    progress = 0;
    error = null;
    notifyListeners();
    try {
      final path = await _download(info);
      _downloadPath = path;
      status = UpdateStatus.ready;
      // open_filex не умеет передавать extraArgs (например, '-r' для
      // pm install); обновление идёт по стандартному Intent-установщику.
      // Повторная установка поверх старой версии требует повышения
      // versionCode в собираемом APK.
      await OpenFilex.open(
        path,
        type: 'application/vnd.android.package-archive',
      );
    } on Exception catch (e) {
      status = UpdateStatus.error;
      error = '$e';
    }
    notifyListeners();
  }

  Future<String> _download(LatestInfo info) async {
    final client = http.Client();
    final req = http.Request(
        'GET', Uri.parse('$updateBaseUrl/update/${info.filename}'));
    req.headers['Authorization'] = 'Bearer ${await _token()}';
    final streamed = await client.send(req);
    if (streamed.statusCode != 200) {
      client.close();
      throw HttpException('download: HTTP ${streamed.statusCode}');
    }
    final dir = await getDownloadsDirectory() ?? await getTemporaryDirectory();
    final file = File('${dir.path}/${info.filename}');
    final sink = file.openWrite();
    final total = info.size > 0 ? info.size : null;
    var received = 0;
    try {
      // NOTE: Stream.drain в актуальном SDK не принимает onData-колбэк
      // (лишь futureValue), поэтому обходим поток явно через await for —
      // иначе чанки в файл записаны не будут.
      await for (final chunk in streamed.stream) {
        sink.add(chunk);
        if (total != null) {
          received += chunk.length;
          progress = (received / total).clamp(0.0, 1.0).toDouble();
          notifyListeners();
        }
      }
    } finally {
      // Гарантированно закрываем синк, даже если загрузка оборвалась.
      await sink.close();
    }
    client.close();
    progress = 1.0;
    notifyListeners();

    // sha256 — локальный хэш цельности (не crypto-версионирование); если в
    // манифесте нет — пропускаем, не роняем установку.
    if (info.sha256.isNotEmpty) {
      // Digest.toString() — нижний hex; сравниваем строки, а не объекты
      // (Digest != String в Dart был бы всегда true).
      final hex = (await sha256.bind(file.openRead()).first).toString();
      if (hex != info.sha256) {
        await file.delete();
        throw Exception('sha256 mismatch: $hex != ${info.sha256}');
      }
    }
    return file.path;
  }

  @override
  void dispose() {
    final p = _downloadPath;
    _downloadPath = null;
    if (p != null && status != UpdateStatus.ready) {
      File(p).delete().then((_) {}, onError: (Object _) {});
    }
    super.dispose();
  }
}