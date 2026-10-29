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
import java.time.*
import kotlinx.coroutines.launch

@Composable
fun TodayScreen(entities: List<Entity>, create: () -> Unit) {
    val context = LocalContext.current
    val repo = context.app.repository
    val scope = rememberCoroutineScope()
    val reminders = entities.filter { it.kind == "reminder" }.associate { it.id to it.json.json() }
    val start = LocalDate.now().atStartOfDay(ZoneId.systemDefault()).toInstant()
    val tomorrow = LocalDate.now().plusDays(1).atStartOfDay(ZoneId.systemDefault()).toInstant()
    val occurrences =
        entities
            .filter {
                it.kind == "occurrence" &&
                    it.json.json().str("state") in listOf("scheduled", "fired", "snoozed", "missed")
            }
            .filter {
                val o = it.json.json()
                o.str("state") != "fired" ||
                    reminders[o.str("reminder_id")]?.bool("requires_ack") != false
            }
            .filter {
                runCatching { Instant.parse(it.json.json().str("scheduled_at")) < tomorrow }
                    .getOrDefault(false)
            }
            .sortedBy { it.json.json().str("scheduled_at") }
    var snoozeId by remember { mutableStateOf<String?>(null) }
    if (snoozeId != null)
        AlertDialog(
            onDismissRequest = { snoozeId = null },
            title = { Text("Snooze occurrence") },
            text = {
                Column {
                    listOf(5, 10, 15, 30, 60).forEach { minutes ->
                        TextButton(
                            onClick = {
                                val id = snoozeId!!
                                snoozeId = null
                                scope.launch { repo.action(id, "snooze", minutes) }
                            },
                        ) {
                            Text("$minutes minutes")
                        }
                    }
                }
            },
            confirmButton = {},
            dismissButton = { TextButton(onClick = { snoozeId = null }) { Text("Cancel") } },
        )
    LazyColumn(
        Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { PermissionOnboarding() }
        if (occurrences.isEmpty())
            item {
                EmptyState(
                    "A little breathing room",
                    "Nothing else is due today. Your future reminders are in Reminders.",
                )
                Button(onClick = create, modifier = Modifier.padding(top = 12.dp)) {
                    Text("Create a reminder")
                }
            }
        items(occurrences, key = { it.id }) { row ->
            val o = row.json.json()
            val r = reminders[o.str("reminder_id")] ?: obj()
            val due = runCatching {
                Instant.parse(o.str("scheduled_at"))
            }
                .getOrDefault(Instant.EPOCH)
            val overdue = due < Instant.now()
            ElevatedCard(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(
                            if (overdue) "OVERDUE" else "UPCOMING",
                            style = MaterialTheme.typography.labelMedium,
                            color =
                                if (overdue) MaterialTheme.colorScheme.error
                                else MaterialTheme.colorScheme.primary,
                        )
                        Text(
                            r.str("delivery_mode", "normal").uppercase(),
                            style = MaterialTheme.typography.labelMedium,
                        )
                    }
                    Text(r.str("title", "Reminder"), style = MaterialTheme.typography.titleLarge)
                    if (r.str("body").isNotBlank())
                        Text(r.str("body"), style = MaterialTheme.typography.bodyMedium)
                    Text(
                        displayTime(o.str("scheduled_at")),
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Text(
                        "${o.str("state").replaceFirstChar(Char::uppercase)} · ${if (o.bool("deliver_on_this_device")) "This phone" else "Another device"}",
                        style = MaterialTheme.typography.labelMedium,
                    )
                    if (r.child("source_agent").str("name").isNotBlank())
                        Text(
                            "From ${r.child("source_agent").str("name")}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    Row {
                        TextButton(
                            onClick = { scope.launch { repo.action(row.id, "acknowledge") } },
                        ) {
                            Text("Acknowledge")
                        }
                        TextButton(onClick = { snoozeId = row.id }) { Text("Snooze") }
                        TextButton(onClick = { scope.launch { repo.action(row.id, "skip") } }) {
                            Text("Dismiss")
                        }
                    }
                }
            }
        }
        item { Spacer(Modifier.height(12.dp)) }
    }
}
