package com.errorfactory.agentping

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.errorfactory.agentping.data.*
import com.google.gson.JsonObject
import kotlinx.coroutines.launch

@Composable
fun DevicesScreen(entities: List<Entity>) {
    val context = LocalContext.current
    val repo = context.app.repository
    val scope = rememberCoroutineScope()
    var error by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var rename by remember { mutableStateOf<JsonObject?>(null) }
    var name by remember { mutableStateOf("") }
    var remove by remember { mutableStateOf<JsonObject?>(null) }
    suspend fun update(id: String, body: JsonObject) {
        busy = true
        error = ""
        try {
            repo.api.device(id, body)
            repo.refresh()
        } catch (e: Exception) {
            error = friendlyError(e)
        } finally {
            busy = false
        }
    }
    if (rename != null)
        AlertDialog(
            onDismissRequest = { rename = null },
            title = { Text("Rename device") },
            text = { OutlinedTextField(name, { name = it }, label = { Text("Device name") }) },
            confirmButton = {
                TextButton(
                    enabled = name.isNotBlank() && !busy,
                    onClick = {
                        scope.launch {
                            update(rename!!.str("id"), obj("name" to name))
                            rename = null
                        }
                    },
                ) {
                    Text("Save")
                }
            },
            dismissButton = { TextButton(onClick = { rename = null }) { Text("Cancel") } },
        )
    if (remove != null)
        AlertDialog(
            onDismissRequest = { remove = null },
            title = { Text("Remove ${remove!!.str("name")}?") },
            text = {
                Text(
                    "Its credential will be revoked. An offline phone removes cached alarms when it reconnects. It can be paired again with a new code.",
                )
            },
            confirmButton = {
                TextButton(
                    enabled = !busy,
                    onClick = {
                        scope.launch {
                            busy = true
                            try {
                                val id = remove!!.str("id")
                                repo.api.removeDevice(id)
                                if (id == context.app.secrets.deviceId) repo.forgetLocally()
                                else repo.refresh()
                                remove = null
                            } catch (e: Exception) {
                                error = friendlyError(e)
                                remove = null
                            } finally {
                                busy = false
                            }
                        }
                    },
                ) {
                    Text("Remove device")
                }
            },
            dismissButton = { TextButton(onClick = { remove = null }) { Text("Keep device") } },
        )
    LazyColumn(
        Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        item {
            Text(
                "One primary phone rings alarms by default. Other modes can be mirrored to the selected phones.",
                style = MaterialTheme.typography.bodyMedium,
            )
            ErrorMessage(error)
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
        }
        val rows = entities.filter {
            it.kind == "device" && it.json.json().str("status") == "active"
        }
        if (rows.isEmpty())
            item {
                EmptyState(
                    "No registered phones",
                    "Pair this phone to register it with your local server.",
                )
            }
        items(rows, key = { it.id }) { row ->
            val d = row.json.json()
            ElevatedCard(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text(
                        d.str("name") +
                            if (row.id == context.app.secrets.deviceId) " · This phone" else "",
                        style = MaterialTheme.typography.titleLarge,
                    )
                    Text(
                        "${if (d.bool("is_primary")) "Primary" else "Secondary"} · ${d.child("health").str("state")}",
                    )
                    Text(
                        "${d.str("model")} · Android ${d.str("os_version")}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Text(
                        "Last sync: ${displayTime(d.str("last_sync_at"))}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    val issues =
                        d.child("health").array("issues").map { it.asString.replace('_', ' ') }
                    if (issues.isNotEmpty())
                        Text(
                            issues.joinToString("\n"),
                            color = MaterialTheme.colorScheme.error,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    Row {
                        if (!d.bool("is_primary"))
                            TextButton(
                                enabled = !busy,
                                onClick = {
                                    scope.launch { update(row.id, obj("is_primary" to true)) }
                                },
                            ) {
                                Text("Make primary")
                            }
                        TextButton(
                            enabled = !busy,
                            onClick = {
                                rename = d
                                name = d.str("name")
                            },
                        ) {
                            Text("Rename")
                        }
                        TextButton(enabled = !busy, onClick = { remove = d }) { Text("Remove") }
                    }
                    Text("Alert modes", style = MaterialTheme.typography.titleMedium)
                    Alerts.modes.forEach { mode ->
                        SettingToggle(
                            mode.replaceFirstChar(Char::uppercase),
                            value = d.child("mode_settings").bool(mode, true),
                            enabled = !busy,
                        ) { enabled ->
                            scope.launch {
                                val modes =
                                    obj(
                                            *Alerts.modes
                                                .map {
                                                    it to d.child("mode_settings").bool(it, true)
                                                }
                                                .toTypedArray(),
                                        )
                                        .apply { addProperty(mode, enabled) }
                                update(row.id, obj("mode_settings" to modes))
                            }
                        }
                    }
                    Text(
                        "Notifications: ${d.child("capabilities").bool("notifications_permitted")} · Exact alarms: ${d.child("capabilities").bool("exact_alarms_permitted")}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }
        item { Spacer(Modifier.height(12.dp)) }
    }
}
