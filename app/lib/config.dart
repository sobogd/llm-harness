const String serverUrl = String.fromEnvironment(
    'SERVER_URL',
    defaultValue: 'https://llm.iq-factura.com');

const String authToken = String.fromEnvironment(
    'AUTH_TOKEN',
    defaultValue: '');

/// Публичный базовый URL, откуда приложение качает APK:
///   GET $UPDATE_BASE_URL/update/manifest  -> {version, filename, size, sha256}
///   GET $UPDATE_BASE_URL/update/`<filename>` -> APK
/// Пустая строка отключает проверку обновлений (так в dev-сборках).
/// Выставляется при сборке: --dart-define=UPDATE_BASE_URL=...
const String updateBaseUrl = String.fromEnvironment('UPDATE_BASE_URL',
    defaultValue: '');

/// Текущая версия приложения = счётчик сборки (1, 2, 3, ...), вшит при
/// сборке: --dart-define=APP_VERSION=`<N>`. Сравнивается с /update/manifest.
const String appVersion = String.fromEnvironment('APP_VERSION',
    defaultValue: '0');