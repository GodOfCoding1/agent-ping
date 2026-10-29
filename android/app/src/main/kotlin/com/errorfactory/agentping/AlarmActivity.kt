package com.errorfactory.agentping

import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.errorfactory.agentping.data.*
import kotlinx.coroutines.launch

class AlarmActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        val id = intent.getStringExtra("id") ?: return finish()
        setContent {
            AgentPingTheme {
                var title by remember { mutableStateOf("AgentPing alarm") }
                var body by remember { mutableStateOf("") }
                val scope = rememberCoroutineScope()
                LaunchedEffect(id) {
                    val o = app.repository.store.get("occurrence", id)?.json?.json()
                    val r = o?.let {
                        app.repository.store.get("reminder", it.str("reminder_id"))?.json?.json()
                    }
                    title =
                        r?.str("title")
                            ?: app.repository.store
                                .get("push", id)
                                ?.json
                                ?.json()
                                ?.str("title", "AgentPing alarm")
                            ?: "AgentPing alarm"
                    body = r?.str("body") ?: ""
                    val privacy =
                        app.repository.store
                            .get("settings", "current")
                            ?.json
                            ?.json()
                            ?.child("preferences")
                            ?.str("lock_screen_visibility", "private") ?: "private"
                    if (
                        getSystemService(android.app.KeyguardManager::class.java)
                            .isKeyguardLocked && privacy != "public"
                    ) {
                        title = "AgentPing alarm"
                        body = "Unlock your phone to view reminder details."
                    }
                }
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.surface) {
                    Column(
                        Modifier.padding(32.dp),
                        verticalArrangement =
                            Arrangement.spacedBy(24.dp, Alignment.CenterVertically),
                        horizontalAlignment = Alignment.CenterHorizontally,
                    ) {
                        Text("ALARM", style = MaterialTheme.typography.labelLarge)
                        Text(title, style = MaterialTheme.typography.headlineLarge)
                        Text(body)
                        Button(
                            onClick = {
                                scope.launch {
                                    app.repository.action(id, "snooze")
                                    finish()
                                }
                            },
                            modifier = Modifier.fillMaxWidth(),
                        ) {
                            Text("Snooze 10 minutes")
                        }
                        Button(
                            onClick = {
                                scope.launch {
                                    app.repository.action(id, "acknowledge")
                                    finish()
                                }
                            },
                            modifier = Modifier.fillMaxWidth(),
                        ) {
                            Text("Acknowledge")
                        }
                        OutlinedButton(
                            onClick = {
                                scope.launch {
                                    app.repository.action(id, "skip")
                                    finish()
                                }
                            },
                        ) {
                            Text("Dismiss this occurrence")
                        }
                    }
                }
            }
        }
    }
}
