package com.errorfactory.agentping

import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.errorfactory.agentping.data.*
import java.time.*
import java.time.format.DateTimeFormatter

@Composable
fun AgentPingTheme(content: @Composable () -> Unit) {
    val context = LocalContext.current
    val preference by remember { AppPreferences(context) }.theme.collectAsState("system")
    val dark = preference == "dark" || (preference == "system" && isSystemInDarkTheme())
    val colors =
        if (Build.VERSION.SDK_INT >= 31) {
            if (dark) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        } else if (dark) darkColorScheme() else lightColorScheme()
    MaterialTheme(colorScheme = colors, content = content)
}

fun displayTime(value: String, zone: String = ZoneId.systemDefault().id): String = runCatching {
    Instant.parse(value)
        .atZone(ZoneId.of(zone))
        .format(DateTimeFormatter.ofPattern("EEE, d MMM · h:mm a"))
}
    .getOrDefault(value.ifBlank { "Not yet" })

fun friendlyError(error: Exception): String =
    when ((error as? retrofit2.HttpException)?.code()) {
        401 -> "Device authorization expired or was revoked. Pair this phone again."
        403 -> "This action is not permitted. Check your device and alert permissions."
        409 -> "This changed on another device. Sync and try again."
        429 -> "Too many requests. Wait a moment before trying again."
        400,
        422 -> "Check the values and try again."
        else -> "Cannot reach the server. Cached information is available; retry when connected."
    }

@Composable
fun EmptyState(title: String, description: String) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleLarge)
            Text(description, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

@Composable
fun ErrorMessage(message: String, retry: (() -> Unit)? = null) {
    if (message.isBlank()) return
    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer),
    ) {
        Column(Modifier.padding(12.dp)) {
            Text(message)
            if (retry != null) TextButton(onClick = retry) { Text("Retry") }
        }
    }
}

@Composable
fun SettingToggle(
    title: String,
    description: String = "",
    value: Boolean,
    enabled: Boolean = true,
    onChange: (Boolean) -> Unit,
) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Column(Modifier.weight(1f).padding(end = 12.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            if (description.isNotBlank())
                Text(description, style = MaterialTheme.typography.bodySmall)
        }
        Switch(value, onChange, enabled = enabled)
    }
}
