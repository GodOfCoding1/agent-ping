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
import kotlinx.coroutines.launch

@Composable
fun ActivityScreen(entities: List<Entity>) {
    val repo = LocalContext.current.app.repository
    val scope = rememberCoroutineScope()
    var category by remember { mutableStateOf("all") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf("") }
    val cursor =
        entities
            .firstOrNull { it.kind == "meta" && it.id == "activity:$category" }
            ?.json
            ?.json()
            ?.str("next_cursor")
    suspend fun load(more: Boolean = false) {
        busy = true
        error = ""
        try {
            val page =
                repo.api.activity(if (more) cursor else null, category.takeUnless { it == "all" })
            page.array("items").forEach {
                val value = it.asJsonObject
                repo.store.put(Entity("activity", value.str("id"), value.toString()))
            }
            repo.store.put(
                Entity(
                    "meta",
                    "activity:$category",
                    obj("next_cursor" to page.str("next_cursor")).toString(),
                ),
            )
        } catch (e: Exception) {
            error = friendlyError(e)
        } finally {
            busy = false
        }
    }
    LaunchedEffect(category) { load() }
    val rows =
        entities
            .filter { it.kind == "activity" }
            .map { it.json.json() }
            .filter { category == "all" || it.str("category") == category }
            .sortedByDescending { it.str("occurred_at") }
    LazyColumn(
        Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            FilterMenu(
                "Category",
                category,
                listOf(
                    "all",
                    "reminder",
                    "delivery",
                    "action",
                    "error",
                    "device",
                    "agent",
                    "security",
                ),
            ) {
                category = it
            }
            OutlinedButton(enabled = !busy, onClick = { scope.launch { load() } }) {
                Text("Refresh activity")
            }
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            ErrorMessage(error) { scope.launch { load() } }
        }
        if (rows.isEmpty() && !busy)
            item {
                EmptyState(
                    "No activity yet",
                    "Reminder changes, delivery attempts, device receipts and actions appear here.",
                )
            }
        items(rows, key = { it.str("id") }) { value ->
            OutlinedCard(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(value.str("summary"), style = MaterialTheme.typography.bodyLarge)
                    Text(
                        displayTime(value.str("occurred_at")),
                        style = MaterialTheme.typography.bodySmall,
                    )
                    val actor = value.child("actor")
                    Text(
                        "${value.str("event_type")} · ${actor.str("agent_name").ifBlank { actor.str("device_name").ifBlank { actor.str("kind") } }}",
                        style = MaterialTheme.typography.labelSmall,
                    )
                }
            }
        }
        items(
            entities
                .filter { it.kind == "local_activity" }
                .sortedByDescending { it.json.json().str("occurred_at") },
        ) { row ->
            Text(
                "On this phone: ${row.json.json().str("summary")}",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        if (!cursor.isNullOrEmpty())
            item {
                OutlinedButton(enabled = !busy, onClick = { scope.launch { load(true) } }) {
                    Text("Load older activity")
                }
            }
        item { Spacer(Modifier.height(12.dp)) }
    }
}
