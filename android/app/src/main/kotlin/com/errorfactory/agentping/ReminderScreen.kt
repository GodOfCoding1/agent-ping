package com.errorfactory.agentping

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.errorfactory.agentping.data.*
import com.google.gson.JsonObject
import kotlinx.coroutines.launch

@Composable
fun ReminderScreen(entities: List<Entity>, edit: (JsonObject?) -> Unit) {
    var search by remember { mutableStateOf("") }
    var state by remember { mutableStateOf("all") }
    var mode by remember { mutableStateOf("all") }
    var source by remember { mutableStateOf("all") }
    var selected by remember { mutableStateOf<JsonObject?>(null) }
    val rows = entities.filter { it.kind == "reminder" }.map { it.json.json() }
    if (selected != null)
        ReminderDetails(
            selected!!,
            { selected = null },
            {
                val r = selected
                selected = null
                edit(r)
            },
        )
    LazyColumn(
        Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Button(onClick = { edit(null) }, modifier = Modifier.fillMaxWidth()) {
                Text("New reminder")
            }
            OutlinedTextField(
                search,
                { search = it },
                label = { Text("Search title, description or agent") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
            )
        }
        item {
            FilterMenu(
                "State",
                state,
                listOf("all", "active", "paused", "completed", "cancelled"),
            ) {
                state = it
            }
            FilterMenu("Type", mode, listOf("all", "normal", "persistent", "alarm", "critical")) {
                mode = it
            }
            FilterMenu("Source", source, listOf("all", "manual", "agent")) { source = it }
        }
        val filtered =
            rows
                .filter {
                    (state == "all" || it.str("status") == state) &&
                        (mode == "all" || it.str("delivery_mode") == mode) &&
                        (source == "all" ||
                            (source == "agent") == (it.str("created_by") == "agent")) &&
                        (it.str("title") + it.str("body") + it.child("source_agent").str("name"))
                            .contains(search, true)
                }
                .sortedBy { it.str("next_occurrence_at", "9999") }
        if (filtered.isEmpty())
            item {
                EmptyState(
                    "No matching reminders",
                    "Create a reminder or change the search and filters.",
                )
            }
        items(filtered, key = { it.str("id") }) { r ->
            ElevatedCard(Modifier.fillMaxWidth().clickable { selected = r }) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(r.str("title"), style = MaterialTheme.typography.titleLarge)
                    Text(
                        "${r.str("delivery_mode").replaceFirstChar(Char::uppercase)} · ${r.str("status")} · ${r.child("source_agent").str("name", "You")}",
                        style = MaterialTheme.typography.labelMedium,
                    )
                    Text(
                        displayTime(r.str("next_occurrence_at")),
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    if (r.child("schedule").str("kind") == "recurring")
                        Text(
                            r.child("schedule").str("rrule"),
                            style = MaterialTheme.typography.bodySmall,
                        )
                }
            }
        }
        item { Spacer(Modifier.height(12.dp)) }
    }
}

@Composable
fun FilterMenu(label: String, value: String, values: List<String>, changed: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Box {
        TextButton(onClick = { expanded = true }) {
            Text("$label: ${value.replaceFirstChar(Char::uppercase)} ▾")
        }
        DropdownMenu(expanded, { expanded = false }) {
            values.forEach { item ->
                DropdownMenuItem(
                    text = { Text(item.replaceFirstChar(Char::uppercase)) },
                    onClick = {
                        changed(item)
                        expanded = false
                    },
                )
            }
        }
    }
}

@Composable
fun ReminderDetails(reminder: JsonObject, close: () -> Unit, edit: () -> Unit) {
    val repo = LocalContext.current.app.repository
    val scope = rememberCoroutineScope()
    var detail by remember { mutableStateOf<JsonObject?>(null) }
    var error by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var cancel by remember { mutableStateOf(false) }
    LaunchedEffect(reminder.str("id")) {
        try {
            detail = repo.api.detail(reminder.str("id"))
        } catch (e: Exception) {
            error = friendlyError(e)
        }
    }
    val r = detail?.child("reminder") ?: reminder
    if (cancel)
        AlertDialog(
            onDismissRequest = { cancel = false },
            title = { Text("Cancel future occurrences?") },
            text = {
                Text(
                    "All synchronized devices will remove future alerts. Offline devices reconcile when they reconnect.",
                )
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        scope.launch {
                            busy = true
                            try {
                                repo.api.cancel(r.str("id"))
                                repo.refresh()
                                close()
                            } catch (e: Exception) {
                                error = friendlyError(e)
                            } finally {
                                busy = false
                                cancel = false
                            }
                        }
                    },
                ) {
                    Text("Cancel reminder")
                }
            },
            dismissButton = { TextButton(onClick = { cancel = false }) { Text("Keep reminder") } },
        )
    AlertDialog(
        onDismissRequest = close,
        title = { Text(r.str("title")) },
        text = {
            Column(
                Modifier.heightIn(max = 450.dp)
                    .verticalScroll(androidx.compose.foundation.rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                Text(r.str("body"))
                Text(
                    "${r.str("delivery_mode")} · ${r.str("status")} · revision ${r.int("revision")}",
                )
                Text("Timezone: ${r.str("timezone")} (${r.str("timezone_mode")})")
                Text(
                    "Targets: ${r.child("targets").str("strategy")} · ${r.child("targets").array("resolved_device_ids").size()} devices",
                )
                Text("Schedule: ${r.child("schedule").str("rrule", "One time")}")
                Text("Next: ${displayTime(r.str("next_occurrence_at"))}")
                detail?.array("recent")?.forEach { value ->
                    val o = value.asJsonObject
                    Text(
                        "${displayTime(o.str("scheduled_at"))} · ${o.str("state")}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    o.array("deliveries").forEach { delivery ->
                        val d = delivery.asJsonObject
                        Text(
                            "${d.str("state")} · ${d.int("push_attempts")} dispatch attempts",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
                ErrorMessage(error)
                if (busy) LinearProgressIndicator()
                if (r.str("status") in listOf("active", "paused")) {
                    OutlinedButton(onClick = edit, enabled = !busy) {
                        Text("Edit schedule and details")
                    }
                    OutlinedButton(
                        enabled = !busy,
                        onClick = {
                            scope.launch {
                                busy = true
                                try {
                                    repo.api.update(
                                        r.str("id"),
                                        obj(
                                            "expected_revision" to r.int("revision"),
                                            "status" to
                                                if (r.str("status") == "active") "paused"
                                                else "active",
                                        ),
                                    )
                                    repo.refresh()
                                    close()
                                } catch (e: Exception) {
                                    error = friendlyError(e)
                                } finally {
                                    busy = false
                                }
                            }
                        },
                    ) {
                        Text(if (r.str("status") == "active") "Pause" else "Resume")
                    }
                    TextButton(onClick = { cancel = true }, enabled = !busy) {
                        Text("Cancel future occurrences")
                    }
                }
            }
        },
        confirmButton = { TextButton(onClick = close) { Text("Close") } },
    )
}
