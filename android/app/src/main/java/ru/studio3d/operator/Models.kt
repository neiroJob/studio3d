package ru.studio3d.operator

import org.json.JSONObject
import java.net.URI

data class Printer(
    val id: Int, val name: String, val status: String, val label: String,
    val updated: Double, val filename: String, val percent: Int, val remaining: Double?,
    val elapsed: Double, val layer: String, val filament: Boolean?, val light: Boolean?,
    val canLight: Boolean, val camera: Boolean, val printHours: Double, val onHours: Double,
    val temperatures: String, val faults: List<String>, val heatingIdle: List<String>, val enabled: Boolean,
) {
    val attention get() = enabled && (status in listOf("offline", "error", "paused", "finished") || faults.isNotEmpty() || heatingIdle.isNotEmpty())
    companion object {
        fun from(o: JSONObject): Printer {
            val job = o.optJSONObject("job")
            val f = o.optJSONObject("filament")
            val faults = o.optJSONArray("faults")
            val heating = o.optJSONArray("heating_idle")
            val temps = o.optJSONObject("temps")
            val names = mapOf("nozzle" to "Сопло", "bed" to "Стол", "chamber" to "Камера")
            return Printer(
                o.getInt("id"), o.getString("name"), o.getString("status"), o.getString("label"),
                o.optDouble("updated", 0.0), job?.optString("filename") ?: "",
                ((job?.optDouble("progress", 0.0) ?: 0.0) * 100).toInt().coerceIn(0, 100),
                if (job != null && !job.isNull("remaining")) job.optDouble("remaining") else null,
                job?.optDouble("elapsed", 0.0) ?: 0.0,
                if (job != null && !job.isNull("layer")) "${job.optInt("layer")} / ${if (job.optInt("total_layers") > 0) job.optInt("total_layers").toString() else "—"}" else "Нет данных",
                if (f == null || f.isNull("present")) null else f.optBoolean("present"),
                if (o.isNull("light")) null else o.optBoolean("light"), o.optBoolean("can_light"),
                o.optBoolean("camera"), o.optDouble("print_hours"), o.optDouble("on_hours"),
                names.mapNotNull { (key, label) -> temps?.optJSONArray(key)?.let { "$label ${it.optDouble(0).toInt()} / ${it.optDouble(1).toInt()} °C" } }.joinToString("\n"),
                if (faults == null) emptyList() else (0 until faults.length()).map { faults.getString(it) },
                if (heating == null) emptyList() else (0 until heating.length()).map { heating.getString(it) },
                o.optBoolean("enabled", true),
            )
        }
    }
}

data class Fleet(val studio: String, val name: String, val printers: List<Printer>) {
    companion object {
        fun from(o: JSONObject): Fleet {
            val rows = o.getJSONArray("printers")
            return Fleet(o.optString("studio", "Studio3D"), o.optString("name"),
                (0 until rows.length()).map { Printer.from(rows.getJSONObject(it)) })
        }
    }
}

fun duration(seconds: Double?): String {
    if (seconds == null || !seconds.isFinite()) return "Нет данных"
    val m = (seconds.coerceAtLeast(0.0) / 60).toInt()
    return if (m >= 60) "${m / 60} ч ${m % 60} мин" else "$m мин"
}

fun normalizeServer(input: String, allowLocalHttp: Boolean): String {
    val uri = try { URI(input.trim()) } catch (_: Exception) { throw IllegalArgumentException("Некорректный адрес сервера") }
    val host = uri.host?.lowercase() ?: throw IllegalArgumentException("Укажите полный адрес с https://")
    require(uri.userInfo == null && uri.query == null && uri.fragment == null) { "В адресе не должно быть пароля, параметров или #" }
    require(uri.path.isNullOrBlank() || uri.path == "/") { "Укажите адрес сервера без пути страницы" }
    val parts = host.split('.').mapNotNull { it.toIntOrNull() }
    val privateIp = parts.size == 4 && parts.all { it in 0..255 } &&
        (parts[0] == 10 || (parts[0] == 192 && parts[1] == 168) || (parts[0] == 172 && parts[1] in 16..31) || parts[0] == 127)
    val local = privateIp || host == "localhost" || host.endsWith(".local")
    require(uri.scheme == "https" || (uri.scheme == "http" && allowLocalHttp && local)) {
        "Нужен HTTPS. HTTP разрешён только для локального адреса с включённой опцией ниже"
    }
    return input.trim().trimEnd('/')
}
