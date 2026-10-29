package com.errorfactory.agentping.data

import android.content.Context
import android.os.Build
import androidx.room.withTransaction
import com.errorfactory.agentping.*
import com.google.firebase.messaging.FirebaseMessaging
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import java.time.Instant
import java.time.ZoneId
import java.util.UUID
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

class Repository(
    val context: Context,
    val db: LocalDatabase,
    val secrets: SecureStore,
    val api: AgentPingApi,
) {
    val store = db.store()
    val status = MutableStateFlow("Connect your phone to get started")
    val busy = MutableStateFlow(false)
    val repairRequested = MutableStateFlow(false)

    suspend fun forgetLocally() {
        store.list("alarm").forEach { alarms.cancel(it.json.json().str("occurrence_id")) }
        context.getSystemService(android.app.NotificationManager::class.java).cancelAll()
        RingingService.active?.let { RingingService.stop(context, it) }
        db.withTransaction {
            store.clearEntities()
            store.clearPending()
            store.clearEvents()
        }
        secrets.token = null
        secrets.deviceId = ""
        androidx.work.WorkManager.getInstance(context).cancelAllWork()
        repairRequested.value = true
    }

    private val networkLock = Mutex()
    private val localLock = Mutex()
    val alarms = AlarmScheduler(context, this)
    val paired
        get() = secrets.token != null

    suspend fun pair(code: String, name: String) {
        val result =
            api.pair(
                obj(
                    "pairing_code" to code,
                    "installation_id" to secrets.installationId,
                    "name" to name,
                    "model" to Build.MODEL,
                    "os_version" to Build.VERSION.RELEASE,
                    "app_version" to BuildConfig.VERSION_NAME,
                    "timezone" to ZoneId.systemDefault().id,
                    "capabilities" to Alerts.capabilities(context),
                ),
            )
        if (secrets.deviceId.isNotEmpty() && secrets.deviceId != result.child("device").str("id"))
            forgetLocally()
        secrets.deviceId = result.child("device").str("id")
        secrets.token = result.str("device_token").also { require(it.startsWith("apd_")) }
        store.put(Entity("device", secrets.deviceId, result.child("device").toString()))
        repairRequested.value = false
        status.value = "Paired securely • enable notifications for delivery"
    }

    fun registerPush() {
        if (!BuildConfig.FIREBASE_CONFIGURED || !paired || !Alerts.permitted(context)) return
        context.app.scope.launch {
            try {
                FirebaseMessaging.getInstance().isAutoInitEnabled = true
                FirebaseMessaging.getInstance().register().addOnFailureListener {
                    status.value = "Firebase registration failed • retry in Settings"
                }
            } catch (error: Exception) {
                if (error is kotlinx.coroutines.CancellationException) throw error
                status.value = "Firebase registration failed • retry in Settings"
            }
        }
    }

    suspend fun updateRegistration() {
        if (!paired) return
        api.updateSelf(
            obj(
                "push_token" to secrets.fid,
                "push_token_kind" to "fid",
                "capabilities" to Alerts.capabilities(context),
                "timezone" to ZoneId.systemDefault().id,
                "app_version" to BuildConfig.VERSION_NAME,
                "os_version" to Build.VERSION.RELEASE,
            ),
        )
    }

    suspend fun receipt(id: String, version: Int, kind: String, detail: String? = null) {
        val key = "$id:$version:$kind"
        store.queue(
            Pending(
                key,
                "receipt",
                obj(
                        "occurrence_id" to id,
                        "occurrence_version" to version,
                        "kind" to kind,
                        "at" to Instant.now().toString(),
                        "detail" to detail,
                    )
                    .toString(),
                System.currentTimeMillis(),
            ),
        )
    }

    suspend fun flushReceipts() {
        store
            .pending()
            .filter { it.kind == "receipt" }
            .chunked(100)
            .forEach { batch ->
                api.receipts(
                    obj("receipts" to JsonArray().apply { batch.forEach { add(it.json.json()) } }),
                )
                batch.forEach { store.dequeue(it.id) }
            }
    }

    suspend fun syncReplica(forceFull: Boolean = false) {
        var cursor =
            if (forceFull) null else store.get("meta", "sync")?.json?.json()?.get("cursor")?.asLong
        do {
            val page = api.sync(cursor)
            localLock.withLock { Replica(db).apply(page) }
            cursor = page.get("cursor").asLong
        } while (page.bool("has_more"))
    }

    suspend fun flushActions() {
        store
            .pending()
            .filter { it.kind == "action" }
            .chunked(100)
            .forEach { batch ->
                val result =
                    api.actions(
                        obj(
                            "actions" to
                                JsonArray().apply { batch.forEach { add(it.json.json()) } },
                        ),
                    )
                db.withTransaction {
                    result.forEach { element ->
                        val value = element.asJsonObject
                        val id = value.str("client_action_id")
                        if (batch.none { it.id == id }) return@forEach
                        val occurrence = value.child("occurrence")
                        if (occurrence.has("id")) {
                            val old =
                                store.get("occurrence", occurrence.str("id"))?.json?.json()
                                    ?: JsonObject()
                            occurrence.entrySet().forEach { (key, item) -> old.add(key, item) }
                            store.put(Entity("occurrence", old.str("id"), old.toString()))
                        }
                        store.put(
                            Entity(
                                "local_activity",
                                id,
                                obj(
                                        "summary" to
                                            ("Action " +
                                                value.str("result") +
                                                " " +
                                                value.str("reason")),
                                        "occurred_at" to Instant.now().toString(),
                                    )
                                    .toString(),
                            ),
                        )
                        store.dequeue(id)
                    }
                }
            }
    }

    suspend fun action(
        id: String,
        action: String,
        minutes: Int = 10,
        source: String = "device_app",
    ) = localLock.withLock {
        require(action in listOf("acknowledge", "snooze", "skip"))
        require(minutes in 1..720)
        var queued = false
        db.withTransaction {
            val occurrence =
                store.get("occurrence", id)?.json?.json()
                    ?: store.get("push", id)?.json?.json()?.let {
                        obj(
                            "id" to id,
                            "version" to it.str("occurrence_version").toInt(),
                            "reminder_id" to it.str("reminder_id"),
                            "state" to "fired",
                        )
                    }
                    ?: return@withTransaction
            if (
                occurrence.str("state") in
                    listOf("cancelled", "superseded", "acknowledged", "skipped")
            )
                return@withTransaction
            if (occurrence.str("state") == "missed" && action == "snooze") {
                status.value = "Expired reminders cannot be snoozed"
                return@withTransaction
            }
            val reminder = store.get("reminder", occurrence.str("reminder_id"))?.json?.json()
            if (
                action == "snooze" &&
                    occurrence.int("snooze_count") >=
                        (reminder?.child("snooze")?.int("max_snoozes", 5) ?: 5)
            ) {
                status.value = "Snooze limit reached"
                return@withTransaction
            }
            val at = Instant.now()
            val actionId = UUID.randomUUID().toString()
            store.queue(
                Pending(
                    actionId,
                    "action",
                    obj(
                            "client_action_id" to actionId,
                            "occurrence_id" to id,
                            "action" to action,
                            "occurred_at" to at.toString(),
                            "source" to source,
                            "snooze_minutes" to if (action == "snooze") minutes else null,
                            "reason" to if (action == "skip") "Dismissed on device" else null,
                        )
                        .toString(),
                    System.currentTimeMillis(),
                ),
            )
            occurrence.addProperty(
                "state",
                when (action) {
                    "acknowledge" -> "acknowledged"
                    "skip" -> "skipped"
                    else -> "snoozed"
                },
            )
            if (action == "snooze") {
                occurrence.addProperty("scheduled_at", at.plusSeconds(minutes * 60L).toString())
                occurrence.addProperty("snoozed_until", at.plusSeconds(minutes * 60L).toString())
                occurrence.addProperty("snooze_count", occurrence.int("snooze_count") + 1)
                val oldExpiry = runCatching {
                    Instant.parse(occurrence.str("expires_at"))
                }
                    .getOrDefault(at)
                val newExpiry =
                    at.plusSeconds(
                        minutes * 60L + (reminder?.int("expire_after_minutes", 60) ?: 60) * 60L,
                    )
                occurrence.addProperty("expires_at", maxOf(oldExpiry, newExpiry).toString())
                occurrence.addProperty("local_action", actionId)
            }
            store.put(Entity("occurrence", id, occurrence.toString()))
            queued = true
        }
        if (!queued) return@withLock
        Alerts.cancel(context, id)
        RingingService.stop(context, id)
        alarms.cancel(id)
        alarms.rebuild()
        status.value = "Action saved on phone • synchronization queued"
        SyncWork.enqueue(context)
    }

    suspend fun refresh(): Boolean = networkLock.withLock {
        if (!paired) return@withLock true
        busy.value = true
        try {
            updateRegistration()
            flushReceipts()
            flushActions()
            syncReplica()
            alarms.rebuild()
            flushReceipts()
            store.list("occurrence").forEach {
                if (
                    it.json.json().str("state") in
                        listOf(
                            "acknowledged",
                            "skipped",
                            "cancelled",
                            "superseded",
                            "missed",
                            "snoozed",
                        )
                ) {
                    Alerts.cancel(context, it.id)
                    RingingService.stop(context, it.id)
                }
            }
            status.value = "Connected • ${java.time.LocalTime.now().withNano(0)}"
            true
        } catch (e: Exception) {
            if (e is kotlinx.coroutines.CancellationException) throw e
            status.value =
                if ((e as? retrofit2.HttpException)?.code() == 401)
                    "Device credential expired or revoked • pair again"
                else "Offline • changes will retry when connected"
            (e as? retrofit2.HttpException)?.code() == 401
        } finally {
            busy.value = false
        }
    }

    suspend fun fire(id: String, version: Int, due: Long, step: Int) = localLock.withLock {
        val o = store.get("occurrence", id)?.json?.json() ?: return@withLock
        val r = store.get("reminder", o.str("reminder_id"))?.json?.json() ?: return@withLock
        if (
            o.int("version") != version ||
                Instant.parse(o.str("scheduled_at")).toEpochMilli() != due
        )
            return@withLock
        val settings = store.get("settings", "current")?.json?.json() ?: obj()
        val quiet = settings.child("preferences").child("quiet_hours")
        val quietEnd =
            if (
                r.str("delivery_mode") == "normal" &&
                    r.bool("respect_quiet_hours", true) &&
                    quiet.str("behavior") == "defer"
            )
                runCatching {
                    QuietPolicy.end(
                            Instant.ofEpochMilli(due),
                            settings.str("timezone", ZoneId.systemDefault().id),
                            quiet.bool("enabled"),
                            quiet.str("start", "22:00"),
                            quiet.str("end", "07:00"),
                        )
                        ?.toEpochMilli()
                }
                    .getOrNull()
            else null
        if (quietEnd != null && System.currentTimeMillis() < quietEnd) {
            alarms.rebuild()
            return@withLock
        }
        val expires = Instant.parse(o.str("expires_at")).toEpochMilli()
        if (
            !AlarmPolicy.eligible(
                o.str("state"),
                r.str("status") == "active",
                o.bool("deliver_on_this_device"),
                quietEnd ?: due,
                expires,
                System.currentTimeMillis(),
                r.int("late_grace_minutes", 10),
            )
        ) {
            if (System.currentTimeMillis() >= expires) receipt(id, version, "skipped_late")
            return@withLock
        }
        val payload =
            obj(
                "v" to "1",
                "type" to "deliver",
                "occurrence_id" to id,
                "occurrence_version" to version,
                "reminder_id" to r.str("id"),
                "mode" to r.str("delivery_mode"),
                "scheduled_at" to o.str("scheduled_at"),
                "expires_at" to o.str("expires_at"),
                "title" to r.str("title"),
                "body" to r.str("body"),
                "escalation_step" to step,
                "snooze_minutes" to r.child("snooze").int("default_minutes", 10),
                "actions" to r.array("actions"),
            )
        if (
            r.str("delivery_mode") == "normal" &&
                r.bool("respect_quiet_hours", true) &&
                quiet.str("behavior") == "silent" &&
                runCatching {
                    QuietPolicy.end(
                        Instant.now(),
                        settings.str("timezone", ZoneId.systemDefault().id),
                        quiet.bool("enabled"),
                        quiet.str("start", "22:00"),
                        quiet.str("end", "07:00"),
                    )
                }
                    .getOrNull() != null
        )
            payload.addProperty("silent", "1")
        dispatch(id, version, payload)
        alarms.rebuild()
        SyncWork.enqueue(context)
    }

    private suspend fun dispatch(id: String, version: Int, payload: JsonObject) {
        val settings = store.get("settings", "current")?.json?.json() ?: obj()
        val preferences = settings.child("preferences")
        if (payload.str("mode") == "critical" && !preferences.bool("critical_alerts_enabled")) {
            receipt(id, version, "failed", "Critical alerts are not enabled on this phone")
            return
        }
        val device = store.get("device", secrets.deviceId)?.json?.json()
        if (
            device != null &&
                (!device.child("mode_settings").bool(payload.str("mode"), true) ||
                    device.str("status") == "deactivated")
        )
            return
        val definition = store.get("reminder", payload.str("reminder_id"))?.json?.json()
        if (!payload.has("snooze_minutes"))
            payload.addProperty(
                "snooze_minutes",
                definition
                    ?.child("snooze")
                    ?.int("default_minutes", preferences.int("default_snooze_minutes", 10))
                    ?: preferences.int("default_snooze_minutes", 10),
            )
        payload.addProperty("visibility", preferences.str("lock_screen_visibility", "private"))
        payload.addProperty("vibrate", preferences.bool("vibrate", true))
        val due = Instant.parse(payload.str("scheduled_at")).toEpochMilli()
        val key = AlarmPolicy.displayKey(id, version, due, payload.int("escalation_step"))
        if (store.claim(Event(key, id, version, "claimed", System.currentTimeMillis())) == -1L) {
            if (store.event(key)?.phase == "displayed") return
            val existing =
                context
                    .getSystemService(android.app.NotificationManager::class.java)
                    .activeNotifications
                    .any { it.notification.extras.getString("agentping_event") == key }
            if (existing) {
                store.phase(key, "displayed")
                receipt(id, version, "displayed")
                return
            }
        }
        payload.addProperty("display_key", key)
        store.put(Entity("push", id, payload.toString()))
        if (store.get("occurrence", id) == null) {
            store.put(
                Entity(
                    "occurrence",
                    id,
                    obj(
                            "id" to id,
                            "reminder_id" to payload.str("reminder_id"),
                            "version" to version,
                            "state" to "fired",
                            "scheduled_at" to payload.str("scheduled_at"),
                            "expires_at" to payload.str("expires_at"),
                            "deliver_on_this_device" to true,
                        )
                        .toString(),
                ),
            )
        }
        if (store.get("reminder", payload.str("reminder_id")) == null) {
            store.put(
                Entity(
                    "reminder",
                    payload.str("reminder_id"),
                    obj(
                            "id" to payload.str("reminder_id"),
                            "revision" to payload.str("reminder_revision", "1"),
                            "status" to "active",
                            "title" to payload.str("title", "AgentPing"),
                            "body" to payload.str("body"),
                            "delivery_mode" to payload.str("mode"),
                            "requires_ack" to (payload.str("requires_ack") == "1"),
                            "late_grace_minutes" to 10,
                        )
                        .toString(),
                ),
            )
        }
        if (Alerts.show(context, id, payload)) {
            store.phase(key, "displayed")
            receipt(id, version, "displayed")
            if (payload.str("mode") == "alarm" && payload.str("silent") != "1") {
                try {
                    androidx.core.content.ContextCompat.startForegroundService(
                        context,
                        android.content
                            .Intent(context, RingingService::class.java)
                            .putExtra("id", id)
                            .putExtra("payload", payload.toString()),
                    )
                } catch (_: RuntimeException) {
                    receipt(
                        id,
                        version,
                        "failed",
                        "Android blocked background ringing; visible notification posted",
                    )
                }
            }
            status.value = "Notification posted on this phone"
        } else {
            store.phase(key, "blocked")
            receipt(id, version, "failed", "Notifications or channel disabled")
        }
    }

    suspend fun receive(data: Map<String, String>) = localLock.withLock {
        if (!paired || data["v"] != "1") return@withLock
        val id = data["occurrence_id"] ?: return@withLock
        val version = data["occurrence_version"]?.toIntOrNull() ?: return@withLock
        val local = store.get("occurrence", id)?.json?.json()
        if (data["type"] == "dismiss") {
            if (local == null || local.int("version") <= version) {
                Alerts.cancel(context, id)
                RingingService.stop(context, id)
                alarms.cancel(id)
                if (local != null) {
                    local.addProperty("state", data["state"] ?: "cancelled")
                    local.addProperty("version", version)
                    store.put(Entity("occurrence", id, local.toString()))
                }
            }
            return@withLock
        }
        if (data["type"] != "deliver") return@withLock
        receipt(id, version, "received")
        if (
            local != null &&
                (local.int("version") > version || local.str("state") in AlarmPolicy.terminal)
        )
            return@withLock
        if (
            store.pending().any { it.kind == "action" && it.json.json().str("occurrence_id") == id }
        )
            return@withLock
        val expires = runCatching {
            Instant.parse(data["expires_at"]).toEpochMilli()
        }
            .getOrDefault(0)
        if (expires <= System.currentTimeMillis()) {
            receipt(id, version, "skipped_late")
            return@withLock
        }
        val due =
            runCatching { Instant.parse(data["scheduled_at"]).toEpochMilli() }.getOrNull()
                ?: return@withLock
        if (
            local != null &&
                local.int("version") == version &&
                Instant.parse(local.str("scheduled_at")).toEpochMilli() != due
        )
            return@withLock
        val payload = obj(*data.map { it.key to it.value }.toTypedArray())
        dispatch(id, version, payload)
    }
}
