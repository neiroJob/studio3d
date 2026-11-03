package ru.studio3d.operator

import android.app.Application
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.time.OffsetDateTime
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            val colors = if (isSystemInDarkTheme()) darkColorScheme(primary = Color(0xff75aaff))
                else lightColorScheme(primary = Color(0xff237aef), background = Color(0xfff5f7fb))
            MaterialTheme(colorScheme = colors) { Surface(Modifier.fillMaxSize()) { StudioApp() } }
        }
    }
}

class StudioModel(app: Application) : AndroidViewModel(app) {
    private val vault = Vault(app)
    private var historyVersion = 0
    private var eventsVersion = 0
    var server by mutableStateOf(""); private set
    var token by mutableStateOf(""); private set
    var fleet by mutableStateOf<Fleet?>(null); private set
    var error by mutableStateOf<String?>(null); private set
    var notice by mutableStateOf<String?>(null); private set
    var busy by mutableStateOf(false); private set
    var stale by mutableStateOf(true); private set
    var demo by mutableStateOf(false); private set
    var interval by mutableIntStateOf(10)
    var eventRows by mutableStateOf<List<JSONObject>>(emptyList()); private set
    var historyRows by mutableStateOf<List<JSONObject>>(emptyList()); private set
    var historyError by mutableStateOf<String?>(null); private set
    var eventsError by mutableStateOf<String?>(null); private set
    var eventsBusy by mutableStateOf(false); private set
    var historyBusy by mutableStateOf(false); private set
    val api get() = Api(server, token)
    val connected get() = token.isNotBlank() || demo

    init {
        vault.get("session")?.let { raw -> runCatching {
            val data = JSONObject(raw); server = data.getString("server"); token = data.getString("token")
            vault.get("fleet")?.let { fleet = Fleet.from(JSONObject(it)) }
        }.onFailure { vault.clear(); server = ""; token = "" } }
    }

    private fun friendly(e: Exception): String = when (e) {
        is ApiError -> e.message ?: "Ошибка сервера"
        is IllegalArgumentException -> e.message ?: "Проверьте введённые данные"
        else -> "Нет связи с сервером. Проверьте адрес и подключение к сети"
    }

    fun login(address: String, username: String, password: String, local: Boolean) = viewModelScope.launch {
        busy = true; error = null
        try {
            val normalized = normalizeServer(address, local)
            val result = Api(normalized).json("login/", JSONObject().put("username", username)
                .put("password", password).put("device", "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}"))
            server = normalized; token = result.getString("token"); demo = false
            vault.put("session", JSONObject().put("server", server).put("token", token).toString())
            refresh()
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { error = friendly(e) }
        finally { busy = false }
    }

    suspend fun refresh() {
        if (demo || token.isBlank()) return
        val requestToken = token
        try {
            val data = api.json("fleet/")
            if (token != requestToken || demo) return
            fleet = Fleet.from(data); stale = false; error = null
            vault.put("fleet", data.toString())
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) {
            if (token != requestToken || demo) return
            stale = true
            if (e is ApiError && e.status in listOf(401, 403)) { token = ""; vault.clear(); fleet = null }
            error = friendly(e)
        }
    }

    fun refreshNow() = viewModelScope.launch { busy = true; refresh(); busy = false }
    fun openHistory(id: Int) = viewModelScope.launch {
        val version = ++historyVersion
        val requestToken = token
        historyRows = emptyList(); historyError = null; historyBusy = true
        try {
            if (demo) return@launch
            val rows = api.json("printers/$id/history/").getJSONArray("jobs")
            if (version == historyVersion && token == requestToken)
                historyRows = (0 until rows.length()).map { rows.getJSONObject(it) }
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { if (version == historyVersion && token == requestToken) historyError = friendly(e) }
        finally { if (version == historyVersion) historyBusy = false }
    }
    fun openEvents() = viewModelScope.launch {
        val version = ++eventsVersion
        val requestToken = token
        eventRows = emptyList(); eventsError = null; eventsBusy = true
        try {
            if (demo) {
                eventRows = listOf(JSONObject().put("printer", "Creality K1").put("title", "Печать завершена — снимите изделие").put("at", "Пример события"))
            } else {
                val rows = api.json("events/").getJSONArray("events")
                if (version == eventsVersion && token == requestToken)
                    eventRows = (0 until rows.length()).map { rows.getJSONObject(it) }
            }
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { if (version == eventsVersion && token == requestToken) eventsError = friendly(e) }
        finally { if (version == eventsVersion) eventsBusy = false }
    }
    fun light(printer: Printer) = viewModelScope.launch {
        if (demo || busy || stale) return@launch
        val requestToken = token
        busy = true; notice = null
        try {
            api.json("printers/${printer.id}/light/", JSONObject().put("on", printer.light != true))
            if (token != requestToken) return@launch
            notice = "${printer.name}: команда света выполнена"; refresh()
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { if (token == requestToken) notice = friendly(e) }
        finally { if (token == requestToken) busy = false }
    }
    fun logout() = viewModelScope.launch {
        val previous = api
        demo = false; token = ""; fleet = null; error = null; stale = true; vault.clear()
        historyVersion++; eventsVersion++; historyRows = emptyList(); eventRows = emptyList()
        historyBusy = false; eventsBusy = false; busy = false; notice = null
        runCatching { previous.json("logout/", JSONObject()) }
    }
    fun demo() {
        demo = true; stale = false; error = null
        val rows = JSONArray()
        listOf("Creality K1C-1", "Creality K1C-2", "Creality K1", "QIDI Plus 4", "QIDI Q1 Pro").forEachIndexed { i, name ->
            val status = listOf("printing", "printing", "finished", "ready", "offline")[i]
            rows.put(JSONObject().put("id", i+1).put("name", name).put("status", status)
                .put("label", listOf("Печать", "Печать", "Ждёт снятия", "Готов", "Нет связи")[i])
                .put("updated", Instant.now().epochSecond).put("can_light", false).put("camera", false)
                .put("print_hours", 128.4).put("on_hours", 205.0).put("filament", JSONObject().put("present", true))
                .put("job", if (i < 3) JSONObject().put("filename", "primer_PETG.gcode").put("progress", listOf(.64, .38, 1.0)[i])
                    .put("remaining", 4680).put("elapsed", 3600) else JSONObject.NULL))
        }
        fleet = Fleet.from(JSONObject().put("studio", "Studio3D").put("name", "Демонстрация").put("printers", rows))
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable fun StudioApp(model: StudioModel = viewModel()) {
    if (!model.connected) { LoginScreen(model); return }
    var page by remember { mutableIntStateOf(0) }
    var selected by remember { mutableStateOf<Int?>(null) }
    var tab by remember { mutableIntStateOf(0) }
    val lifecycle = LocalLifecycleOwner.current
    LaunchedEffect(model.token, model.demo, model.interval) {
        lifecycle.lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (true) { model.refresh(); delay(model.interval * 1000L) }
        }
    }
    LaunchedEffect(page) { if (page == 2) model.openEvents() }
    BackHandler(selected != null) { selected = null; tab = 0 }
    val printers = model.fleet?.printers.orEmpty()
    val printer = printers.firstOrNull { it.id == selected }
    Scaffold(
        topBar = { TopAppBar(title = { Column {
            Text("STUDIO3D · ОПЕРАТОР", fontSize = 11.sp, color = MaterialTheme.colorScheme.primary)
            Text(printer?.name ?: listOf("Вся студия", "Камеры", "События", "Настройки")[page], fontWeight = FontWeight.Bold)
        } }, navigationIcon = { if (selected != null) IconButton(onClick = { selected = null; tab = 0 }) { Icon(Icons.Outlined.ArrowBack, "Назад") } },
            actions = { IconButton(onClick = { model.refreshNow() }, enabled = !model.busy) { Icon(Icons.Outlined.Refresh, "Обновить") } }) },
        bottomBar = { NavigationBar {
            listOf(Icons.Outlined.Dashboard, Icons.Outlined.CameraAlt, Icons.Outlined.Notifications, Icons.Outlined.Settings)
                .forEachIndexed { i, icon -> NavigationBarItem(selected = page == i && selected == null,
                    onClick = { page = i; selected = null; tab = 0 }, icon = { Icon(icon, null) },
                    label = { Text(listOf("Обзор", "Камеры", "События", "Настройки")[i]) }) }
        } },
    ) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            if (model.demo) Notice("Демонстрационные данные · команды отключены")
            if (model.stale) Notice("Нет связи с сервером · данные могут быть устаревшими", true)
            model.error?.let { Notice(it, true) }
            model.notice?.let { Notice(it) }
            if (model.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            if (printer != null) {
                PrinterDetail(model, printer, tab, { tab = it; if (it == 1) model.openHistory(printer.id) })
            } else when (page) {
                0 -> Overview(printers, model.fleet?.studio ?: "Studio3D") { selected = it; tab = 0 }
                1 -> Cameras(model, printers) { selected = it; tab = 0 }
                2 -> Events(model)
                3 -> Settings(model)
            }
        }
    }
}

@Composable private fun LoginScreen(model: StudioModel) {
    var server by remember { mutableStateOf(model.server) }
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var local by remember { mutableStateOf(false) }
    LazyColumn(Modifier.fillMaxSize().safeDrawingPadding().imePadding().padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item { Spacer(Modifier.height(24.dp)); Icon(Icons.Outlined.Print, null, Modifier.size(48.dp), tint = MaterialTheme.colorScheme.primary) }
        item { Text("Studio3D", fontSize = 34.sp, fontWeight = FontWeight.Bold); Text("Ваша студия — под контролем", color = MaterialTheme.colorScheme.onSurfaceVariant) }
        item { OutlinedTextField(server, { server = it }, label = { Text("Адрес сервера") }, placeholder = { Text("https://studio.example.com") }, modifier = Modifier.fillMaxWidth(), singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri)) }
        item { OutlinedTextField(username, { username = it }, label = { Text("Логин Studio3D") }, modifier = Modifier.fillMaxWidth(), singleLine = true) }
        item { OutlinedTextField(password, { password = it }, label = { Text("Пароль") }, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth(), singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password)) }
        item { Row(verticalAlignment = Alignment.CenterVertically) { Checkbox(local, { local = it }); Text("Разрешить HTTP для локальной сети", fontSize = 13.sp) } }
        item { model.error?.let { Notice(it, true) } }
        item { Button(onClick = { model.login(server, username, password, local) }, enabled = !model.busy && server.isNotBlank() && username.isNotBlank() && password.isNotBlank(), modifier = Modifier.fillMaxWidth()) { Text(if (model.busy) "Подключение…" else "Войти") } }
        item { OutlinedButton(onClick = { model.demo() }, modifier = Modifier.fillMaxWidth()) { Text("Посмотреть демонстрацию") } }
        item { Text("Нужен сервер Studio3D с мобильным API. Для доступа из интернета настройте внешний HTTPS-адрес. Локальный адрес вне студии недоступен.", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant) }
    }
}

@Composable private fun Notice(message: String, warning: Boolean = false) {
    Surface(color = if (warning) MaterialTheme.colorScheme.errorContainer else MaterialTheme.colorScheme.secondaryContainer,
        shape = RoundedCornerShape(12.dp), modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 5.dp)) {
        Text(message, modifier = Modifier.padding(12.dp), fontSize = 13.sp)
    }
}

@Composable private fun Overview(printers: List<Printer>, studio: String, open: (Int) -> Unit) {
    LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { Text(studio, style = MaterialTheme.typography.titleMedium) }
        item { Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Metric("Принтеров", printers.size.toString()); Metric("Печатают", printers.count { it.enabled && it.status == "printing" }.toString()); Metric("Внимание", printers.count { it.attention }.toString())
        } }
        if (printers.any { it.status == "finished" }) item { Text("Изделие готово к снятию", color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold) }
        if (printers.isEmpty()) item { Text("Принтеров пока нет. Добавьте их в Studio3D или обновите соединение.") }
        items(printers, key = { it.id }) { p -> PrinterRow(p) { open(p.id) } }
    }
}

@Composable private fun Metric(label: String, value: String) { Column { Text(value, fontSize = 30.sp, fontWeight = FontWeight.Bold); Text(label, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant) } }

@Composable private fun PrinterRow(printer: Printer, open: () -> Unit) {
    OutlinedCard(onClick = open, modifier = Modifier.fillMaxWidth()) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Icon(if (printer.status == "offline") Icons.Outlined.WifiOff else Icons.Outlined.Print, null, tint = MaterialTheme.colorScheme.primary)
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(printer.name, fontWeight = FontWeight.Bold)
                Text(if (!printer.enabled) "Опрос выключен" else printer.label + if (printer.status == "printing") " · ${printer.percent} %" else "", fontSize = 13.sp)
                if (printer.enabled && printer.status in listOf("printing", "paused")) {
                    LinearProgressIndicator(progress = { printer.percent / 100f }, modifier = Modifier.fillMaxWidth())
                    Text("Осталось: ${duration(printer.remaining)}", fontSize = 12.sp)
                }
                if (printer.faults.isNotEmpty()) Text(printer.faults.first(), fontSize = 12.sp, color = MaterialTheme.colorScheme.error, maxLines = 2, overflow = TextOverflow.Ellipsis)
                if (printer.heatingIdle.isNotEmpty()) Text("Нагрев без печати", fontSize = 12.sp, color = MaterialTheme.colorScheme.error)
            }
            Icon(Icons.Outlined.ChevronRight, null)
        }
    }
}

@Composable private fun Cameras(model: StudioModel, printers: List<Printer>, open: (Int) -> Unit) {
    LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item { Text("Снимки камер · обновление вручную", fontSize = 13.sp) }
        items(printers, key = { it.id }) { p -> Card(Modifier.fillMaxWidth()) {
            Row(Modifier.fillMaxWidth().clickable { open(p.id) }.padding(14.dp), horizontalArrangement = Arrangement.SpaceBetween) { Text(p.name, fontWeight = FontWeight.Bold); Text(p.label, fontSize = 12.sp) }
            CameraImage(model, p)
        } }
    }
}

@Composable private fun CameraImage(model: StudioModel, printer: Printer) {
    var reload by remember(printer.id) { mutableIntStateOf(0) }
    var photo by remember(printer.id) { mutableStateOf<Photo?>(null) }
    var error by remember(printer.id) { mutableStateOf<String?>(null) }
    var loading by remember(printer.id) { mutableStateOf(false) }
    LaunchedEffect(printer.id, reload, model.demo) {
        if (model.demo || !printer.camera || printer.status == "offline") return@LaunchedEffect
        loading = true; error = null
        try { photo = model.api.photo(printer.id) }
        catch (e: CancellationException) { throw e }
        catch (e: Exception) { error = if (e is ApiError) e.message else "Не удалось получить кадр" }
        finally { loading = false }
    }
    Box(Modifier.fillMaxWidth().aspectRatio(16f/10f).background(MaterialTheme.colorScheme.surfaceVariant), contentAlignment = Alignment.Center) {
        photo?.let { Image(it.bitmap.asImageBitmap(), "Кадр ${printer.name}", Modifier.fillMaxSize(), contentScale = androidx.compose.ui.layout.ContentScale.Fit) }
        if (photo == null) Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.padding(20.dp)) {
            Icon(Icons.Outlined.Videocam, null, Modifier.size(36.dp)); Spacer(Modifier.height(8.dp))
            Text(when { model.demo -> "Камера · демонстрация"; loading -> "Получение кадра…"; !printer.camera -> "Камера не настроена"; printer.status == "offline" -> "Принтер не в сети"; else -> error ?: "Снимок недоступен" }, fontSize = 13.sp)
        }
        if (loading) CircularProgressIndicator(Modifier.size(28.dp))
    }
    photo?.let { Text("Снимок: ${formatTime(it.at)}", Modifier.padding(horizontal = 12.dp, vertical = 6.dp), fontSize = 11.sp) }
    if (photo != null && error != null) Text(error.orEmpty(), Modifier.padding(12.dp), color = MaterialTheme.colorScheme.error, fontSize = 12.sp)
    TextButton(onClick = { reload++ }, enabled = !loading && !model.demo && printer.camera && printer.status != "offline") { Icon(Icons.Outlined.Refresh, null); Text("Обновить снимок") }
}

@Composable private fun PrinterDetail(model: StudioModel, p: Printer, tab: Int, selectTab: (Int) -> Unit) {
    Column {
        TabRow(tab) { listOf("Состояние", "История").forEachIndexed { i, label -> Tab(tab == i, { selectTab(i) }, text = { Text(label) }) } }
        if (tab == 1) {
            LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                if (model.historyBusy) item { CircularProgressIndicator() }
                model.historyError?.let { item { Notice(it, true) } }
                if (!model.historyBusy && model.historyRows.isEmpty() && model.historyError == null) item { Text(if (model.demo) "В демонстрации история не загружается" else "Заданий в истории пока нет") }
                items(model.historyRows) { job -> OutlinedCard(Modifier.fillMaxWidth()) { Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(job.optString("filename"), fontWeight = FontWeight.Bold)
                    Text("${job.optString("status")} · ${duration(job.optDouble("duration"))}", fontSize = 13.sp)
                    Text("${formatTime(job.optString("ended_at"))} · ${job.optString("quality")}", fontSize = 12.sp)
                } } }
            }
        } else LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            item { CameraImage(model, p) }
            item { Text(p.label, style = MaterialTheme.typography.headlineSmall) }
            if (p.filename.isNotBlank()) item { Text(p.filename) }
            if (p.status in listOf("printing", "paused")) item {
                Text("${p.percent} % · осталось ${duration(p.remaining)}", fontSize = 22.sp, fontWeight = FontWeight.Bold)
                LinearProgressIndicator(progress = { p.percent / 100f }, modifier = Modifier.fillMaxWidth().padding(top = 10.dp))
                Text("Прошло: ${duration(p.elapsed)} · слой: ${p.layer}", Modifier.padding(top = 8.dp), fontSize = 12.sp)
            }
            item { Text("Нить: ${when (p.filament) { true -> "есть"; false -> "нет"; null -> "нет данных" }}") }
            if (p.temperatures.isNotBlank()) item { Text(p.temperatures) }
            items(p.faults) { Text(it, color = MaterialTheme.colorScheme.error) }
            if (p.heatingIdle.isNotEmpty()) item { Text("Нагрев без печати: ${p.heatingIdle.joinToString()}", color = MaterialTheme.colorScheme.error) }
            item { Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) { Metric("Печать, ч", "%.1f".format(p.printHours)); Metric("Включён, ч", "%.1f".format(p.onHours)) } }
            if (p.canLight) item { Button(onClick = { model.light(p) }, enabled = !model.busy && !model.stale && !model.demo, modifier = Modifier.fillMaxWidth()) { Icon(Icons.Outlined.Lightbulb, null); Spacer(Modifier.width(8.dp)); Text(if (p.light == true) "Выключить свет" else "Включить свет") } }
            item { Text("Данные принтера: ${if (p.updated > 0) formatTime(Instant.ofEpochSecond(p.updated.toLong()).toString()) else "Нет ответа"}", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
    }
}

@Composable private fun Events(model: StudioModel) {
    LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        item { Text("Требует внимания", style = MaterialTheme.typography.titleLarge) }
        if (model.eventsBusy) item { CircularProgressIndicator() }
        model.eventsError?.let { item { Notice(it, true) } }
        if (!model.eventsBusy && model.eventRows.isEmpty() && model.eventsError == null) item { Text("Всё в порядке. Нет событий, требующих внимания.") }
        items(model.eventRows) { row -> OutlinedCard(Modifier.fillMaxWidth()) { Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
            Text(row.optString("printer"), fontWeight = FontWeight.Bold); Text(row.optString("title"))
            if (row.optString("at").isNotBlank()) Text(formatTime(row.optString("at")), fontSize = 12.sp)
        } } }
        item { OutlinedButton(onClick = { model.openEvents() }, enabled = !model.eventsBusy) { Text("Обновить события") } }
    }
}

@Composable private fun Settings(model: StudioModel) {
    LazyColumn(contentPadding = PaddingValues(20.dp), verticalArrangement = Arrangement.spacedBy(18.dp)) {
        item { Text("Аккаунт", style = MaterialTheme.typography.titleLarge); Text(model.fleet?.name.orEmpty()) }
        item { Text("Сервер", fontWeight = FontWeight.Bold); Text(if (model.demo) "Демонстрационный режим" else model.server, fontSize = 13.sp) }
        item { Text("Обновление состояния", fontWeight = FontWeight.Bold); Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) { listOf(5, 10, 30).forEach { seconds -> FilterChip(model.interval == seconds, { model.interval = seconds }, label = { Text("$seconds сек") }) } } }
        item { Text("Без push-уведомлений", fontWeight = FontWeight.Bold); Text("Состояние обновляется, пока приложение открыто. Камеры обновляются вручную.", fontSize = 13.sp) }
        item { Text("Управление", fontWeight = FontWeight.Bold); Text("В приложении доступен только свет. Команды запуска, паузы, отмены и нагрева отсутствуют.", fontSize = 13.sp) }
        item { Text("Версия 0.1.0 · API v1", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp) }
        item { OutlinedButton(onClick = { model.logout() }, modifier = Modifier.fillMaxWidth()) { Text(if (model.demo) "Выйти из демонстрации" else "Выйти и удалить сессию с устройства") } }
    }
}

private fun formatTime(value: String): String = runCatching {
    OffsetDateTime.parse(value).atZoneSameInstant(ZoneId.systemDefault()).format(DateTimeFormatter.ofPattern("dd.MM HH:mm:ss"))
}.getOrDefault(if (value.isBlank() || value == "null") "Нет данных" else value)
