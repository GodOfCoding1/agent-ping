package com.errorfactory.agentping

import android.Manifest
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.errorfactory.agentping.data.*
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onResume() {
        super.onResume()
        if (app.repository.paired) app.repository.registerPush()
        app.scope.launch {
            app.repository.alarms.rebuild()
            if (app.repository.paired) app.repository.refresh()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            AgentPingTheme {
                val repair by app.repository.repairRequested.collectAsState()
                if (app.repository.paired && !repair) Dashboard() else PairingScreen()
            }
        }
    }
}

@Composable
fun PairingScreen() {
    val context = androidx.compose.ui.platform.LocalContext.current
    val repo = remember { context.app.repository }
    val scope = rememberCoroutineScope()
    val status by repo.status.collectAsState()
    var paired by remember { mutableStateOf(repo.paired && !repo.repairRequested.value) }
    var code by remember { mutableStateOf("") }
    var name by remember { mutableStateOf(android.os.Build.MODEL) }
    var error by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    val permission =
        rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
            repo.registerPush()
            SyncWork.enqueue(context)
        }
    if (paired) {
        Dashboard()
        return
    }
    Scaffold { padding ->
        Column(
            Modifier.fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Text("AgentPing", style = MaterialTheme.typography.headlineLarge)
            Text(
                if (paired) "Your phone is connected" else "Connect your phone",
                style = MaterialTheme.typography.headlineSmall,
            )
            Text(status)
            if (!paired) {
                Text(
                    if (BuildConfig.LOCAL_BACKEND)
                        "Generate a one-time pairing code on your local AgentPing server."
                    else
                        "Sign in to your AgentPing account in your browser, create a pairing code, then enter it here. Only this phone’s credential is stored here, encrypted by Android Keystore.",
                )
                Text(
                    "Server: ${java.net.URI(BuildConfig.BACKEND_BASE_URL).host}",
                    style = MaterialTheme.typography.bodySmall,
                )
                if (!BuildConfig.LOCAL_BACKEND) {
                    OutlinedButton(
                        onClick = {
                            context.startActivity(
                                android.content.Intent(
                                    android.content.Intent.ACTION_VIEW,
                                    android.net.Uri.parse(
                                        java.net.URI(BuildConfig.BACKEND_BASE_URL)
                                            .resolve("account")
                                            .toString(),
                                    ),
                                ),
                            )
                        },
                    ) {
                        Text("Open account & get pairing code")
                    }
                }
                OutlinedTextField(
                    name,
                    { name = it },
                    label = { Text("Device name") },
                    singleLine = true,
                )
                OutlinedTextField(
                    code,
                    { code = it },
                    label = { Text("Pairing code") },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                )
                Button(
                    enabled = !busy && code.length >= 6 && name.isNotBlank(),
                    onClick = {
                        scope.launch {
                            busy = true
                            error = ""
                            try {
                                repo.pair(code.trim(), name.trim())
                                paired = true
                                code = ""
                                SyncWork.periodic(context)
                                repo.registerPush()
                                SyncWork.enqueue(context)
                            } catch (e: Exception) {
                                error =
                                    "Pairing failed. Check the code, expiry and server connection."
                            } finally {
                                busy = false
                            }
                        }
                    },
                ) {
                    Text(if (busy) "Connecting…" else "Pair securely")
                }
            } else {
                Text(
                    "Notifications need your permission. You can change channel sound and vibration in Android Settings.",
                )
                Button(
                    onClick = {
                        if (Build.VERSION.SDK_INT >= 33 && !Alerts.permitted(context))
                            permission.launch(Manifest.permission.POST_NOTIFICATIONS)
                        else {
                            repo.registerPush()
                            SyncWork.enqueue(context)
                        }
                    },
                ) {
                    Text("Enable notifications & Firebase")
                }
                OutlinedButton(onClick = { scope.launch { repo.refresh() } }) {
                    Text("Refresh connection")
                }
                Text(
                    "Firebase registration: ${if (context.app.secrets.fid != null) "registered" else "pending"}",
                )
                Text(
                    "Notification permission: ${if (Alerts.permitted(context)) "allowed" else "disabled"}",
                )
            }
            if (error.isNotEmpty()) Text(error, color = MaterialTheme.colorScheme.error)
        }
    }
}
