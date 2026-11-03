package ru.studio3d.operator

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.security.KeyStore
import java.util.concurrent.TimeUnit
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class ApiError(val status: Int, message: String) : Exception(message)
data class Photo(val bitmap: Bitmap, val at: String)

class Vault(context: Context) {
    private val prefs = context.getSharedPreferences("studio3d", Context.MODE_PRIVATE)
    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey("studio3d-session", null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder("studio3d-session", KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    fun put(name: String, value: String) {
        val c = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        prefs.edit().putString(name, Base64.encodeToString(c.iv + c.doFinal(value.toByteArray(Charsets.UTF_8)), Base64.NO_WRAP)).apply()
    }
    fun get(name: String): String? = try {
        prefs.getString(name, null)?.let {
            val raw = Base64.decode(it, Base64.NO_WRAP)
            val c = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, raw.copyOfRange(0, 12))) }
            c.doFinal(raw.copyOfRange(12, raw.size)).toString(Charsets.UTF_8)
        }
    } catch (_: Exception) { null }
    fun clear() { prefs.edit().clear().apply() }
}

class Api(val server: String, val token: String = "") {
    private val http = OkHttpClient.Builder().connectTimeout(10, TimeUnit.SECONDS)
        .callTimeout(45, TimeUnit.SECONDS).followRedirects(false).followSslRedirects(false).build()
    private fun request(path: String, body: JSONObject? = null): Request {
        val builder = Request.Builder().url("$server/api/mobile/v1/$path")
        if (token.isNotBlank()) builder.header("Authorization", "Bearer $token")
        if (body != null) builder.post(body.toString().toRequestBody("application/json".toMediaType()))
        return builder.build()
    }
    suspend fun json(path: String, body: JSONObject? = null): JSONObject = withContext(Dispatchers.IO) {
        http.newCall(request(path, body)).execute().use { r ->
            val text = r.body?.string().orEmpty()
            val data = try { JSONObject(text) } catch (_: Exception) {
                throw ApiError(r.code, "Сервер не поддерживает мобильный API. Обновите Studio3D из dev")
            }
            if (!r.isSuccessful) throw ApiError(r.code, data.optString("error", "Ошибка сервера ${r.code}"))
            data
        }
    }
    suspend fun photo(id: Int): Photo = withContext(Dispatchers.IO) {
        http.newCall(request("printers/$id/photo/")).execute().use { r ->
            if (!r.isSuccessful) {
                val text = r.body?.string().orEmpty()
                throw ApiError(r.code, runCatching { JSONObject(text).optString("error") }.getOrDefault("Камера недоступна"))
            }
            val bytes = r.body?.bytes() ?: throw ApiError(503, "Нет кадра")
            Photo(BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: throw ApiError(503, "Некорректное изображение"), r.header("X-Captured-At").orEmpty())
        }
    }
}
