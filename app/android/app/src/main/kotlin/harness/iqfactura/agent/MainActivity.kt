package harness.iqfactura.agent

import android.content.Intent
import android.net.Uri
import androidx.core.content.FileProvider
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File

class MainActivity : FlutterActivity() {
    private val CHANNEL = "harness/updater"

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "canInstall" ->
                        result.success(packageManager.canRequestPackageInstalls())
                    "openInstallSettings" -> {
                        openInstallSettings()
                        result.success(null)
                    }
                    "installApk" -> {
                        val path = call.argument<String>("path")
                        result.success(installApk(path))
                    }
                    else -> result.notImplemented()
                }
            }
    }

    /** Открывает системную страницу разрешения установки неизвестных приложений. */
    private fun openInstallSettings() {
        val intent = Intent(
            android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
            Uri.parse("package:$packageName")
        )
        try {
            startActivity(intent)
        } catch (_: Exception) {
            // Некоторые ROM-ы не принимают package: uri — открываем общий экран.
            runCatching {
                startActivity(Intent(android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES))
            }
        }
    }

    /**
     * Возвращает 0 при успехе, иначе код ошибки:
     * -1 некорректный путь, -2 файл не найден, -3 не удалось открыть установщик.
     */
    private fun installApk(path: String?): Int {
        if (path.isNullOrEmpty()) return -1
        val file = File(path)
        if (!file.exists()) return -2
        val uri: Uri =
            FileProvider.getUriForFile(this, "$packageName.fileprovider", file)
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        return try {
            startActivity(intent)
            0
        } catch (_: Exception) {
            -3
        }
    }
}