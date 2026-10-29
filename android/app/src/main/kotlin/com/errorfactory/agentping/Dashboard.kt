package com.errorfactory.agentping

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.errorfactory.agentping.data.*
import com.google.gson.JsonObject
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun Dashboard() {
    val context = LocalContext.current
    val repo = context.app.repository
    val scope = rememberCoroutineScope()
    val entities by repo.store.observe().collectAsStateWithLifecycle(initialValue = emptyList())
    val pending by
        repo.store.observePending().collectAsStateWithLifecycle(initialValue = emptyList())
    val status by repo.status.collectAsStateWithLifecycle()
    val busy by repo.busy.collectAsStateWithLifecycle()
    var tab by rememberSaveable { mutableStateOf("Today") }
    var editor by remember { mutableStateOf(false) }
    var editing by remember { mutableStateOf<JsonObject?>(null) }
    if (editor)
        ReminderEditor(
            editing,
            entities
                .firstOrNull { it.kind == "settings" }
                ?.json
                ?.json()
                ?.child("preferences")
                ?.int("default_snooze_minutes", 10) ?: 10,
        ) {
            editor = false
            editing = null
        }
    LaunchedEffect(Unit) {
        SyncWork.periodic(context)
        repo.refresh()
    }
    val tabs =
        listOf(
            "Today" to R.drawable.ic_today,
            "Reminders" to R.drawable.ic_reminders,
            "Activity" to R.drawable.ic_activity,
            "Devices" to R.drawable.ic_devices,
            "Settings" to R.drawable.ic_settings,
        )
    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(
                            "AgentPing",
                            style = MaterialTheme.typography.labelLarge,
                            color = MaterialTheme.colorScheme.primary,
                        )
                        Text(tab, style = MaterialTheme.typography.headlineSmall)
                    }
                },
                actions = {
                    TextButton(enabled = !busy, onClick = { scope.launch { repo.refresh() } }) {
                        Text(if (busy) "Syncing…" else "Sync")
                    }
                },
            )
        },
        bottomBar = {
            NavigationBar {
                tabs.forEach { (label, icon) ->
                    NavigationBarItem(
                        modifier = Modifier.testTag("nav-$label"),
                        selected = tab == label,
                        onClick = { tab = label },
                        icon = {
                            Icon(
                                painterResource(icon),
                                contentDescription = null,
                                modifier = Modifier.size(23.dp),
                            )
                        },
                        label = { Text(label) },
                    )
                }
            }
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            Surface(
                color =
                    if (status.startsWith("Offline") || status.contains("revoked"))
                        MaterialTheme.colorScheme.errorContainer
                    else MaterialTheme.colorScheme.surfaceContainer,
            ) {
                Column(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 8.dp)) {
                    Text(status, style = MaterialTheme.typography.labelMedium)
                    if (pending.isNotEmpty())
                        Text(
                            "${pending.count{it.kind=="action"}} actions · ${pending.count{it.kind=="receipt"}} receipts waiting to sync",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    if (busy) LinearProgressIndicator(Modifier.fillMaxWidth().padding(top = 4.dp))
                }
            }
            Spacer(Modifier.height(12.dp))
            when (tab) {
                "Today" -> TodayScreen(entities) { editor = true }
                "Reminders" ->
                    ReminderScreen(entities) {
                        editing = it
                        editor = true
                    }
                "Activity" -> ActivityScreen(entities)
                "Devices" -> DevicesScreen(entities)
                "Settings" -> SettingsScreen(entities)
            }
        }
    }
}
