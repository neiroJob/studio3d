package ru.studio3d.operator

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class ApiTransportTest {
    @Test fun sendsBearerToVersionedApi() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("{\"studio\":\"Test\",\"printers\":[]}"))
            val api = Api(server.url("/").toString().trimEnd('/'), "synthetic-test-token")
            assertEquals("Test", api.json("fleet/").getString("studio"))
            val request = server.takeRequest()
            assertEquals("/api/mobile/v1/fleet/", request.path)
            assertEquals("Bearer synthetic-test-token", request.getHeader("Authorization"))
        }
    }
    @Test fun loginDoesNotSendBearer() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("{\"token\":\"test-token\"}"))
            Api(server.url("/").toString().trimEnd('/')).json("login/", JSONObject().put("username", "test").put("password", "synthetic-password"))
            val request = server.takeRequest()
            assertNull(request.getHeader("Authorization"))
            assertEquals("POST", request.method)
            assertEquals("test", JSONObject(request.body.readUtf8()).getString("username"))
        }
    }
    @Test fun doesNotFollowRedirectsWithCredentials() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setResponseCode(302).setHeader("Location", "https://example.com/"))
            try { Api(server.url("/").toString().trimEnd('/'), "test-token").json("fleet/"); fail("Expected error") }
            catch (e: ApiError) { assertEquals(302, e.status) }
            assertEquals(1, server.requestCount)
        }
    }
    @Test fun serverErrorsAreVisible() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setResponseCode(401).setBody("{\"error\":\"Login again\"}"))
            try { Api(server.url("/").toString().trimEnd('/')).json("fleet/"); fail("Expected error") }
            catch (e: ApiError) { assertEquals(401, e.status); assertEquals("Login again", e.message) }
        }
    }
    @Test fun preservesUnknownFilamentAndTime() {
        val p = Printer.from(JSONObject("""{"id":1,"name":"Printer","status":"offline","label":"Нет связи","filament":{"present":null},"print_hours":1,"on_hours":2}"""))
        assertNull(p.filament)
        assertNull(p.remaining)
        assertTrue(p.attention)
        assertEquals("Нет данных", duration(p.remaining))
    }
    @Test fun heatingIdleRequiresAttentionButDisabledPollingDoesNot() {
        val raw = JSONObject("""{"id":1,"name":"Printer","status":"ready","label":"Готов","heating_idle":["сопло 230 °C"]}""")
        assertTrue(Printer.from(raw).attention)
        raw.put("enabled", false)
        assertFalse(Printer.from(raw).attention)
    }
}
