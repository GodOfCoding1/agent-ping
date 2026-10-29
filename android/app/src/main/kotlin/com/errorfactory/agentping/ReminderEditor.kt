package com.errorfactory.agentping

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import com.errorfactory.agentping.data.*
import com.google.gson.JsonObject
import java.time.LocalDateTime
import java.time.ZoneId
import kotlinx.coroutines.launch

@Composable
fun ReminderEditor(
    existing: JsonObject? = null,
    defaultSnoozeMinutes: Int = 10,
    onClose: () -> Unit,
) {
    val context = LocalContext.current
    val repo = context.app.repository
    val scope = rememberCoroutineScope()
    var title by rememberSaveable { mutableStateOf(existing?.str("title") ?: "") }
    var body by rememberSaveable { mutableStateOf(existing?.str("body") ?: "") }
    var mode by rememberSaveable { mutableStateOf(existing?.str("delivery_mode") ?: "normal") }
    val schedule = existing?.child("schedule")
    var start by rememberSaveable {
        mutableStateOf(
            schedule
                ?.str("start_local")
                ?.ifBlank { schedule.str("local_datetime").ifBlank { schedule.str("at") } }
                ?.ifBlank { null }
                ?: LocalDateTime.now().plusMinutes(5).withSecond(0).withNano(0).toString(),
        )
    }
    var timezone by rememberSaveable {
        mutableStateOf(existing?.str("timezone") ?: ZoneId.systemDefault().id)
    }
    var recurrence by rememberSaveable {
        mutableStateOf(if (schedule?.str("kind") == "recurring") "Custom" else "Once")
    }
    var rule by rememberSaveable { mutableStateOf(schedule?.str("rrule") ?: "FREQ=DAILY") }
    var count by rememberSaveable { mutableStateOf(schedule?.str("count") ?: "") }
    var until by rememberSaveable { mutableStateOf(schedule?.str("until") ?: "") }
    var strategy by rememberSaveable {
        mutableStateOf(existing?.child("targets")?.str("strategy") ?: "all")
    }
    val entities by repo.store.observe().collectAsState(initial = emptyList())
    var selectedDevices by rememberSaveable {
        mutableStateOf<List<String>>(
            existing?.child("targets")?.array("device_ids")?.map { it.asString } ?: emptyList(),
        )
    }
    var floating by rememberSaveable {
        mutableStateOf(existing?.str("timezone_mode") == "floating")
    }
    var clamp by rememberSaveable { mutableStateOf(schedule?.str("monthly_day_policy") == "clamp") }
    var defaultSnooze by rememberSaveable {
        mutableStateOf(
            (existing?.child("snooze")?.int("default_minutes", 10) ?: defaultSnoozeMinutes)
                .toString(),
        )
    }
    var maxSnoozes by rememberSaveable {
        mutableStateOf((existing?.child("snooze")?.int("max_snoozes", 5) ?: 5).toString())
    }
    var escalate by rememberSaveable {
        mutableStateOf(existing?.child("escalation")?.bool("enabled") ?: false)
    }
    var interval by rememberSaveable {
        mutableStateOf(
            (existing?.child("escalation")?.int("interval_seconds", 300) ?: 300).toString(),
        )
    }
    var repeats by rememberSaveable {
        mutableStateOf((existing?.child("escalation")?.int("max_repeats", 3) ?: 3).toString())
    }
    var expiry by rememberSaveable {
        mutableStateOf((existing?.int("expire_after_minutes", 60) ?: 60).toString())
    }
    var grace by rememberSaveable {
        mutableStateOf((existing?.int("late_grace_minutes", 10) ?: 10).toString())
    }
    var respectQuiet by rememberSaveable {
        mutableStateOf(existing?.bool("respect_quiet_hours", true) ?: true)
    }
    var ack by rememberSaveable { mutableStateOf(existing?.bool("requires_ack") ?: false) }
    var error by rememberSaveable { mutableStateOf("") }
    var busy by rememberSaveable { mutableStateOf(false) }
    val requestKey = remember { java.util.UUID.randomUUID().toString() }
    Dialog(onDismissRequest = { if (!busy) onClose() }) {
        Surface(shape = MaterialTheme.shapes.extraLarge) {
            Column(
                Modifier.heightIn(max = 760.dp)
                    .verticalScroll(rememberScrollState())
                    .padding(20.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Text(
                    if (existing == null) "New reminder" else "Edit reminder",
                    style = MaterialTheme.typography.headlineSmall,
                )
                OutlinedTextField(
                    title,
                    { title = it },
                    label = { Text("Title") },
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true,
                )
                OutlinedTextField(
                    body,
                    { body = it },
                    label = { Text("Description") },
                    modifier = Modifier.fillMaxWidth(),
                )
                Text("Alert type")
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    listOf("normal", "persistent").forEach { value ->
                        FilterChip(
                            mode == value,
                            {
                                mode = value
                                ack = value != "normal"
                                if (existing == null) strategy = "all"
                            },
                            { Text(value.replaceFirstChar(Char::uppercase)) },
                        )
                    }
                }
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    listOf("alarm", "critical").forEach { value ->
                        FilterChip(
                            mode == value,
                            {
                                mode = value
                                ack = true
                                if (existing == null) strategy = "primary"
                            },
                            { Text(value.replaceFirstChar(Char::uppercase)) },
                        )
                    }
                }
                OutlinedTextField(
                    start,
                    { start = it },
                    label = { Text("Local date and time") },
                    supportingText = { Text("YYYY-MM-DDTHH:mm, e.g. 2026-10-30T09:00") },
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    timezone,
                    { timezone = it },
                    label = { Text("IANA timezone") },
                    modifier = Modifier.fillMaxWidth(),
                )
                Row {
                    listOf("Once", "Daily", "Weekly").forEach { value ->
                        FilterChip(recurrence == value, { recurrence = value }, { Text(value) })
                    }
                }
                Row {
                    listOf("Monthly", "Custom").forEach { value ->
                        FilterChip(recurrence == value, { recurrence = value }, { Text(value) })
                    }
                }
                if (recurrence == "Custom")
                    OutlinedTextField(
                        rule,
                        { rule = it },
                        label = { Text("RFC 5545 RRULE") },
                        supportingText = { Text("FREQ=WEEKLY;BYDAY=MO,WE,FR") },
                    )
                if (recurrence != "Once") {
                    OutlinedTextField(
                        count,
                        { count = it },
                        label = { Text("Occurrence count (optional)") },
                    )
                    OutlinedTextField(
                        until,
                        { until = it },
                        label = { Text("End date/time (optional)") },
                        supportingText = { Text("YYYY-MM-DDTHH:mm") },
                    )
                }
                if (recurrence != "Once") {
                    SettingToggle(
                        "Follow home timezone",
                        "Floating schedules follow the primary phone when configured in Settings.",
                        floating,
                    ) {
                        floating = it
                    }
                    SettingToggle(
                        "Clamp missing monthly dates",
                        "Otherwise months without the requested day are skipped.",
                        clamp,
                    ) {
                        clamp = it
                    }
                }
                Text("Receive on")
                Row {
                    listOf("primary", "all", "devices").forEach { value ->
                        FilterChip(
                            strategy == value,
                            { strategy = value },
                            { Text(value.replaceFirstChar(Char::uppercase)) },
                        )
                    }
                }
                if (strategy == "devices")
                    entities
                        .filter { it.kind == "device" && it.json.json().str("status") == "active" }
                        .forEach { row ->
                            SettingToggle(
                                row.json.json().str("name"),
                                value = row.id in selectedDevices,
                            ) { selected ->
                                selectedDevices =
                                    if (selected) selectedDevices + row.id
                                    else selectedDevices - row.id
                            }
                        }
                Row {
                    Checkbox(ack, { ack = it })
                    Text("Require acknowledgement", Modifier.padding(top = 12.dp))
                }
                OutlinedTextField(
                    defaultSnooze,
                    { defaultSnooze = it },
                    label = { Text("Default snooze minutes (1–720)") },
                    singleLine = true,
                )
                OutlinedTextField(
                    maxSnoozes,
                    { maxSnoozes = it },
                    label = { Text("Maximum snoozes (0–20)") },
                    singleLine = true,
                )
                SettingToggle(
                    "Repeat until acknowledged",
                    "Bounded escalation for persistent or critical alerts.",
                    escalate,
                ) {
                    escalate = it
                }
                if (escalate) {
                    OutlinedTextField(
                        interval,
                        { interval = it },
                        label = { Text("Repeat interval seconds (60–3600)") },
                        singleLine = true,
                    )
                    OutlinedTextField(
                        repeats,
                        { repeats = it },
                        label = { Text("Maximum repeats (0–12)") },
                        singleLine = true,
                    )
                }
                OutlinedTextField(
                    expiry,
                    { expiry = it },
                    label = { Text("Expire after minutes (1–10080)") },
                    singleLine = true,
                )
                OutlinedTextField(
                    grace,
                    { grace = it },
                    label = { Text("Late delivery grace minutes (0–1440)") },
                    singleLine = true,
                )
                SettingToggle("Respect quiet hours", "For normal notifications.", respectQuiet) {
                    respectQuiet = it
                }
                if (mode == "critical")
                    Text(
                        "Critical alerts require opt-in in Settings. They follow Android permissions and Do Not Disturb.",
                    )
                if (error.isNotEmpty()) Text(error, color = MaterialTheme.colorScheme.error)
                if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(enabled = !busy, onClick = onClose) { Text("Cancel") }
                    Button(
                        enabled = !busy && title.isNotBlank(),
                        onClick = {
                            scope.launch {
                                busy = true
                                error = ""
                                try {
                                    ZoneId.of(timezone)
                                    require(title.length in 1..200 && body.length <= 4000)
                                    require(
                                        defaultSnooze.toInt() in 1..720 &&
                                            maxSnoozes.toInt() in 0..20,
                                    )
                                    require(expiry.toInt() in 1..10080 && grace.toInt() in 0..1440)
                                    require(
                                        interval.toInt() in 60..3600 &&
                                            repeats.toInt() in 0..12 &&
                                            interval.toInt() * repeats.toInt() <= 21600,
                                    )
                                    require(count.isBlank() || until.isBlank())
                                    require(strategy != "devices" || selectedDevices.isNotEmpty())
                                    val local = runCatching {
                                        LocalDateTime.parse(start)
                                    }
                                        .getOrElse {
                                            java.time.OffsetDateTime.parse(start)
                                                .atZoneSameInstant(ZoneId.of(timezone))
                                                .toLocalDateTime()
                                        }
                                    val newSchedule =
                                        if (recurrence == "Once")
                                            obj(
                                                "kind" to "once",
                                                "local_datetime" to local.toString(),
                                                "timezone" to timezone,
                                            )
                                        else
                                            obj(
                                                "kind" to "recurring",
                                                "start_local" to local.toString(),
                                                "timezone" to timezone,
                                                "rrule" to
                                                    when (recurrence) {
                                                        "Daily" -> "FREQ=DAILY"
                                                        "Weekly" -> "FREQ=WEEKLY"
                                                        "Monthly" -> "FREQ=MONTHLY"
                                                        else -> rule
                                                    },
                                                "timezone_mode" to
                                                    if (floating) "floating" else "fixed",
                                                "monthly_day_policy" to
                                                    if (clamp) "clamp" else "skip",
                                                "count" to
                                                    count.takeIf { it.isNotBlank() }?.toInt(),
                                                "until" to until.takeIf { it.isNotBlank() },
                                            )
                                    val data =
                                        obj(
                                            "title" to title,
                                            "body" to body,
                                            "delivery_mode" to mode,
                                            "schedule" to newSchedule,
                                            "requires_ack" to ack,
                                            "targets" to
                                                if (strategy == "devices")
                                                    obj(
                                                        "strategy" to "devices",
                                                        "device_ids" to
                                                            com.google.gson.JsonArray().apply {
                                                                selectedDevices.forEach { add(it) }
                                                            },
                                                    )
                                                else obj("strategy" to strategy),
                                            "snooze" to
                                                obj(
                                                    "default_minutes" to defaultSnooze.toInt(),
                                                    "max_snoozes" to maxSnoozes.toInt(),
                                                ),
                                            "escalation" to
                                                obj(
                                                    "enabled" to escalate,
                                                    "interval_seconds" to interval.toInt(),
                                                    "max_repeats" to repeats.toInt(),
                                                    "max_duration_seconds" to
                                                        maxOf(
                                                            60,
                                                            interval.toInt() * repeats.toInt(),
                                                        ),
                                                ),
                                            "expire_after_minutes" to expiry.toInt(),
                                            "late_grace_minutes" to grace.toInt(),
                                            "respect_quiet_hours" to respectQuiet,
                                        )
                                    if (existing == null) {
                                        data.addProperty("idempotency_key", requestKey)
                                        repo.api.create(data)
                                    } else {
                                        data.addProperty(
                                            "expected_revision",
                                            existing.int("revision"),
                                        )
                                        repo.api.update(existing.str("id"), data)
                                    }
                                    repo.refresh()
                                    onClose()
                                } catch (e: Exception) {
                                    error =
                                        when ((e as? retrofit2.HttpException)?.code()) {
                                            409 ->
                                                "This reminder changed elsewhere. Close, sync and edit again."
                                            403 ->
                                                "This action is not allowed. Critical alerts need your opt-in."
                                            422,
                                            400 ->
                                                "Check the schedule, recurrence and end/count values."
                                            null ->
                                                if (
                                                    e is java.time.DateTimeException ||
                                                        e is IllegalArgumentException
                                                )
                                                    "Check date/time, numeric limits and device selection. Use either an end time or a count."
                                                else
                                                    "Server unavailable. Your draft is still here; retry to save."
                                            else ->
                                                "Could not save this reminder. Sync and try again."
                                        }
                                } finally {
                                    busy = false
                                }
                            }
                        },
                    ) {
                        Text("Save reminder")
                    }
                }
            }
        }
    }
}
