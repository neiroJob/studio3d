package ru.studio3d.operator

import org.junit.Assert.*
import org.junit.Test

class ServerValidationTest {
    @Test fun acceptsHttps() { assertEquals("https://studio.example.com", normalizeServer(" https://studio.example.com/ ", false)) }
    @Test fun acceptsExplicitLanHttp() { assertEquals("http://192.168.10.2:8080", normalizeServer("http://192.168.10.2:8080", true)) }
    @Test fun rejectsRemoteHttp() { assertThrows(IllegalArgumentException::class.java) { normalizeServer("http://studio.example.com", true) } }
    @Test fun rejectsImplicitLanHttp() { assertThrows(IllegalArgumentException::class.java) { normalizeServer("http://192.168.10.2", false) } }
    @Test fun rejectsCredentials() { assertThrows(IllegalArgumentException::class.java) { normalizeServer("https://user:password@studio.example.com", false) } }
    @Test fun rejectsQueryAndPage() { for (url in listOf("https://studio.example.com/?token=secret", "https://studio.example.com/admin/", "https://studio.example.com/#x")) assertThrows(IllegalArgumentException::class.java) { normalizeServer(url, false) } }
    @Test fun durationUnknownIsExplicit() { assertEquals("Нет данных", duration(null)); assertEquals("Нет данных", duration(Double.NaN)); assertEquals("1 ч 5 мин", duration(3900.0)) }
}
