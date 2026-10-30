package com.errorfactory.agentping

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.errorfactory.agentping.data.*
import com.google.gson.JsonObject
import kotlinx.coroutines.launch

@Composable
fun PermissionOnboarding() {
    val context = LocalContext.current
    val repo = context.app.repository
    val scope = rememberCoroutineScope()
    var allowed by remember { mutableStateOf(Alerts.permitted(context)) }
    val launcher =
        rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            allowed = granted
            if (granted) repo.registerPush()
            SyncWork.enqueue(context)
        }
    if (!allowed)
        Card(
            colors =
                CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.secondaryContainer,
                ),
        ) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Allow reminders to reach you", style = MaterialTheme.typography.titleMedium)
                Text(
                    "AgentPing needs notification permission to show alerts. You can keep using the app without it.",
                    style = MaterialTheme.typography.bodyMedium,
                )
                Button(
                    onClick = {
                        scope.launch { AppPreferences(context).permissionExplained() }
                        if (Build.VERSION.SDK_INT >= 33)
                            launcher.launch(Manifest.permission.POST_NOTIFICATIONS)
                        else
                            context.startActivity(
                                Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                                    .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName),
                            )
                    },
                ) {
                    Text("Enable notifications")
                }
            }
        }
}

@Composable
fun SettingsScreen(entities: List<Entity>) {
    val context = LocalContext.current
    val repo = context.app.repository
    val scope = rememberCoroutineScope()
    val settings = entities.firstOrNull { it.kind == "settings" }?.json?.json() ?: obj()
    val prefs = settings.child("preferences")
    val quiet = prefs.child("quiet_hours")
    var error by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var criticalConfirm by remember { mutableStateOf(false) }
    var pairAgain by remember { mutableStateOf(false) }
    var quietStart by
        remember(settings.int("revision")) { mutableStateOf(quiet.str("start", "22:00")) }
    var quietEnd by remember(settings.int("revision")) { mutableStateOf(quiet.str("end", "07:00")) }
    var timezone by
        remember(settings.int("revision")) {
            mutableStateOf(settings.str("timezone", java.time.ZoneId.systemDefault().id))
        }
    var snooze by
        remember(settings.int("revision")) {
            mutableStateOf(prefs.int("default_snooze_minutes", 10).toString())
        }
    var diagnostics by remember { mutableStateOf<JsonObject?>(null) }
    val localPrefs = remember { AppPreferences(context) }
    val theme by localPrefs.theme.collectAsState("system")
    suspend fun save(change: JsonObject) {
        busy = true
        error = ""
        try {
            change.addProperty("expected_revision", settings.int("revision", 1))
            repo.api.settings(change)
            repo.refresh()
        } catch (e: Exception) {
            error = friendlyError(e)
        } finally {
            busy = false
        }
    }
    suspend fun preference(key: String, value: Any) {
        val updated = prefs.deepCopy()
        obj(key to value).entrySet().forEach { (k, v) -> updated.add(k, v) }
        save(obj("preferences" to updated))
    }
    if (pairAgain)
        AlertDialog(
            onDismissRequest = { pairAgain = false },
            title = { Text("Pair this phone again") },
            text = {
                Text(
                    "Use a new one-time pairing code. The existing credential is replaced only after pairing succeeds.",
                )
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        pairAgain = false
                        repo.repairRequested.value = true
                    },
                ) {
                    Text("Enter pairing code")
                }
            },
            dismissButton = { TextButton(onClick = { pairAgain = false }) { Text("Cancel") } },
        )
    if (criticalConfirm)
        AlertDialog(
            onDismissRequest = { criticalConfirm = false },
            title = { Text("Enable critical alerts?") },
            text = {
                Text(
                    "Authorized agents can send urgent alerts and bounded escalation. Android notification settings and Do Not Disturb still apply. This does not guarantee delivery.",
                )
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        criticalConfirm = false
                        scope.launch { preference("critical_alerts_enabled", true) }
                    },
                ) {
                    Text("Enable critical alerts")
                }
            },
            dismissButton = {
                TextButton(onClick = { criticalConfirm = false }) { Text("Keep disabled") }
            },
        )
    LaunchedEffect(Unit) {
        try {
            repo.api.agents().forEach {
                val a = it.asJsonObject
                repo.store.put(Entity("agent", a.str("id"), a.toString()))
            }
        } catch (e: Exception) {
            error = friendlyError(e)
        }
    }
    LazyColumn(
        Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        item {
            PermissionOnboarding()
            ErrorMessage(error)
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
        }
        item {
            Text("Notification channels", style = MaterialTheme.typography.titleLarge)
            Text(
                "Android controls each channel’s sound, vibration and interruption level. Channel settings override app requests.",
                style = MaterialTheme.typography.bodySmall,
            )
            Alerts.modes.forEach { mode ->
                OutlinedButton(
                    onClick = {
                        context.startActivity(
                            Intent(Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS)
                                .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
                                .putExtra(Settings.EXTRA_CHANNEL_ID, mode),
                        )
                    },
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text("${mode.replaceFirstChar(Char::uppercase)} · sound & vibration")
                }
            }
        }
        item {
            Text("Device health", style = MaterialTheme.typography.titleLarge)
            val caps = Alerts.capabilities(context)
            listOf(
                    "Notifications" to caps.bool("notifications_permitted"),
                    "Exact alarms" to caps.bool("exact_alarms_permitted"),
                    "Full-screen alarms" to caps.bool("full_screen_intent_permitted"),
                    "Do Not Disturb access" to caps.bool("dnd_access_granted"),
                )
                .forEach { (label, value) ->
                    Text("$label: ${if(value) "Allowed" else "Not granted"}")
                }
            Text(
                "Firebase: ${if(context.app.secrets.fid!=null) "Registered (FID)" else "Registration pending"}",
            )
            Text(
                "Battery optimization: ${if(caps.bool("ignoring_battery_optimizations")) "Unrestricted" else "Active"} · Background restriction: ${caps.bool("background_restricted")}",
                style = MaterialTheme.typography.bodySmall,
            )
            Text(
                "Force-stop prevents delivery until you reopen AgentPing. Exact access, notification channels and OEM battery controls can affect alarms.",
                style = MaterialTheme.typography.bodySmall,
            )
            if (Build.VERSION.SDK_INT >= 31)
                OutlinedButton(
                    onClick = {
                        context.startActivity(
                            Intent(
                                Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM,
                                Uri.parse("package:${context.packageName}"),
                            ),
                        )
                    },
                ) {
                    Text("Manage exact alarm access")
                }
            if (Build.VERSION.SDK_INT >= 34)
                OutlinedButton(
                    onClick = {
                        context.startActivity(
                            Intent(
                                Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT,
                                Uri.parse("package:${context.packageName}"),
                            ),
                        )
                    },
                ) {
                    Text("Manage full-screen access")
                }
            OutlinedButton(
                onClick = {
                    context.startActivity(
                        Intent(
                            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                            Uri.parse("package:${context.packageName}"),
                        ),
                    )
                },
            ) {
                Text("App and battery settings")
            }
            OutlinedButton(
                onClick = {
                    repo.registerPush()
                    SyncWork.enqueue(context)
                },
            ) {
                Text("Retry Firebase registration")
            }
            OutlinedButton(
                onClick = {
                    scope.launch {
                        busy = true
                        try {
                            diagnostics = repo.api.diagnostics()
                        } catch (e: Exception) {
                            error = friendlyError(e)
                        } finally {
                            busy = false
                        }
                    }
                },
            ) {
                Text("Check backend diagnostics")
            }
            diagnostics?.let { d ->
                Text(
                    "Backend responded · push ${d.get("push")?.toString() ?: "see Activity"}",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            val sync = entities.firstOrNull { it.kind == "meta" && it.id == "sync" }?.json?.json()
            Text(
                "Last sync: ${displayTime(sync?.str("last_sync") ?: "")}",
                style = MaterialTheme.typography.bodySmall,
            )
            Text(
                "Schedule horizon: ${displayTime(sync?.str("horizon_end") ?: "")}",
                style = MaterialTheme.typography.bodySmall,
            )
            Text(
                "${entities.count {it.kind=="alarm"}} local alarm operations · maximum ${AlarmPolicy.maxAlarms}",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        item {
            Text("Alert preferences", style = MaterialTheme.typography.titleLarge)
            SettingToggle(
                "Critical alerts",
                "Only agents with critical-alert scope can use this mode.",
                prefs.bool("critical_alerts_enabled"),
                !busy,
            ) { enabled ->
                if (enabled) criticalConfirm = true
                else scope.launch { preference("critical_alerts_enabled", false) }
            }
            OutlinedTextField(
                snooze,
                { snooze = it },
                label = { Text("Default snooze minutes (1–720)") },
                singleLine = true,
            )
            TextButton(
                enabled = !busy,
                onClick = {
                    scope.launch {
                        val value = snooze.toIntOrNull()
                        if (value == null || value !in 1..720) error = "Choose 1–720 minutes"
                        else preference("default_snooze_minutes", value)
                    }
                },
            ) {
                Text("Save snooze default")
            }
            FilterMenu(
                "Lock-screen content",
                prefs.str("lock_screen_visibility", "private"),
                listOf("private", "secret", "public"),
            ) { value ->
                scope.launch { preference("lock_screen_visibility", value) }
            }
            FilterMenu(
                "Push content",
                prefs.str("push_content", "full"),
                listOf("full", "minimal"),
            ) { value ->
                scope.launch { preference("push_content", value) }
            }
        }
        item {
            Text("Quiet hours", style = MaterialTheme.typography.titleLarge)
            SettingToggle(
                "Quiet hours for normal alerts",
                "Alarms and urgent modes follow their own channel settings.",
                quiet.bool("enabled"),
                !busy,
            ) { enabled ->
                scope.launch {
                    val q = quiet.deepCopy().apply { addProperty("enabled", enabled) }
                    preference("quiet_hours", q)
                }
            }
            OutlinedTextField(
                quietStart,
                { quietStart = it },
                label = { Text("Start (HH:mm)") },
                singleLine = true,
            )
            OutlinedTextField(
                quietEnd,
                { quietEnd = it },
                label = { Text("End (HH:mm)") },
                singleLine = true,
            )
            FilterMenu(
                "During quiet hours",
                quiet.str("behavior", "silent"),
                listOf("silent", "defer"),
            ) { value ->
                scope.launch {
                    val q = quiet.deepCopy().apply { addProperty("behavior", value) }
                    preference("quiet_hours", q)
                }
            }
            TextButton(
                enabled = !busy,
                onClick = {
                    scope.launch {
                        try {
                            java.time.LocalTime.parse(quietStart)
                            java.time.LocalTime.parse(quietEnd)
                            val q =
                                quiet.deepCopy().apply {
                                    addProperty("start", quietStart)
                                    addProperty("end", quietEnd)
                                }
                            preference("quiet_hours", q)
                        } catch (_: java.time.DateTimeException) {
                            error = "Enter a valid time, such as 22:00."
                        }
                    }
                },
            ) {
                Text("Save quiet hours")
            }
        }
        item {
            Text("Timezone", style = MaterialTheme.typography.titleLarge)
            SettingToggle(
                "Follow primary phone",
                "Fixed-timezone reminders retain their original timezone.",
                settings.str("timezone_source") == "primary_device",
                !busy,
            ) { value ->
                scope.launch {
                    save(obj("timezone_source" to if (value) "primary_device" else "manual"))
                }
            }
            OutlinedTextField(
                timezone,
                { timezone = it },
                label = { Text("Home IANA timezone") },
                singleLine = true,
            )
            TextButton(
                enabled = !busy,
                onClick = {
                    scope.launch {
                        try {
                            java.time.ZoneId.of(timezone)
                            save(obj("timezone" to timezone, "timezone_source" to "manual"))
                        } catch (_: java.time.DateTimeException) {
                            error = "Use an IANA timezone such as Asia/Kolkata."
                        }
                    }
                },
            ) {
                Text("Save home timezone")
            }
            Text(
                "Recurring schedules are expanded by the server with documented DST and missing-month-day policies. Sync after travelling to update floating schedules.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        item {
            Text("Agent permissions", style = MaterialTheme.typography.titleLarge)
            val agents = entities.filter { it.kind == "agent" }
            if (agents.isEmpty()) Text("No agent clients cached. Connect to the server to refresh.")
            agents.forEach { row ->
                val a = row.json.json()
                Text(
                    "${a.str("name")} · ${a.str("status")}",
                    style = MaterialTheme.typography.titleMedium,
                )
                Text(
                    a.array("scopes").joinToString { it.asString },
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            Text(
                "Agents cannot enable critical alerts or change your safety preferences.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        item {
            Text("Appearance and connection", style = MaterialTheme.typography.titleLarge)
            FilterMenu("Theme", theme, listOf("system", "light", "dark")) { value ->
                scope.launch { localPrefs.theme(value) }
            }
            Text(
                "Server: ${BuildConfig.BACKEND_BASE_URL}",
                style = MaterialTheme.typography.bodySmall,
            )
            OutlinedButton(onClick = { pairAgain = true }) { Text("Pair this phone again") }
            Text(
                "Sign in through the account portal to get a one-time pairing code. Device credentials are encrypted with Android Keystore, bound to this server and excluded from backup.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        item { Spacer(Modifier.height(24.dp)) }
    }
}
