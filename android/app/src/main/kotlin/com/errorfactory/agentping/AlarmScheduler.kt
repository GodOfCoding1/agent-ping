package com.errorfactory.agentping

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import com.errorfactory.agentping.data.*
import java.time.Instant

class AlarmScheduler(private val context: Context, private val repo: Repository) {
    private val manager = context.getSystemService(AlarmManager::class.java)

    private fun intent(id: String, version: Int, due: Long, step: Int) =
        PendingIntent.getBroadcast(
            context,
            0,
            Intent(context, AlarmReceiver::class.java)
                .setData(Uri.parse("agentping://alarm/$id/$version/$due/$step"))
                .putExtra("id", id)
                .putExtra("version", version)
                .putExtra("due", due)
                .putExtra("step", step),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

    suspend fun cancel(id: String) {
        repo.store
            .list("alarm")
            .filter { it.json.json().str("occurrence_id") == id }
            .forEach { row ->
                val a = row.json.json()
                manager.cancel(intent(id, a.int("version"), a.get("due").asLong, a.int("step")))
                repo.store.delete("alarm", row.id)
                repo.receipt(id, a.int("version"), "unscheduled")
            }
    }

    suspend fun rebuild() {
        val now = System.currentTimeMillis()
        val settings = repo.store.get("settings", "current")?.json?.json() ?: obj()
        val quiet = settings.child("preferences").child("quiet_hours")
        val reminders = repo.store.list("reminder").associate { it.id to it.json.json() }
        val events = repo.store.events().filter { it.phase == "displayed" }.map { it.id }.toSet()
        val plans =
            repo.store
                .list("occurrence")
                .mapNotNull { row ->
                    val o = row.json.json()
                    val r = reminders[o.str("reminder_id")] ?: return@mapNotNull null
                    if (
                        r.str("delivery_mode") == "critical" &&
                            !settings.child("preferences").bool("critical_alerts_enabled")
                    )
                        return@mapNotNull null
                    if (
                        !Alerts.permitted(context) ||
                            context
                                .getSystemService(android.app.NotificationManager::class.java)
                                .getNotificationChannel(r.str("delivery_mode"))
                                ?.importance == 0
                    )
                        return@mapNotNull null
                    val due =
                        runCatching { Instant.parse(o.str("scheduled_at")).toEpochMilli() }
                            .getOrNull() ?: return@mapNotNull null
                    val quietEnd =
                        if (
                            r.str("delivery_mode") == "normal" &&
                                r.bool("respect_quiet_hours", true) &&
                                quiet.str("behavior") == "defer"
                        )
                            runCatching {
                                QuietPolicy.end(
                                        Instant.ofEpochMilli(due),
                                        settings.str(
                                            "timezone",
                                            java.time.ZoneId.systemDefault().id,
                                        ),
                                        quiet.bool("enabled"),
                                        quiet.str("start", "22:00"),
                                        quiet.str("end", "07:00"),
                                    )
                                    ?.toEpochMilli()
                            }
                                .getOrNull()
                        else null
                    val effectiveDue = quietEnd ?: due
                    val expires = runCatching {
                        Instant.parse(o.str("expires_at")).toEpochMilli()
                    }
                        .getOrDefault(due + 3600000)
                    if (
                        !AlarmPolicy.eligible(
                            o.str("state"),
                            r.str("status") == "active",
                            o.bool("deliver_on_this_device"),
                            effectiveDue,
                            expires,
                            now,
                            r.int("late_grace_minutes", 10),
                        )
                    )
                        return@mapNotNull null
                    val version = o.int("version")
                    val last =
                        (0..12)
                            .filter { AlarmPolicy.displayKey(row.id, version, due, it) in events }
                            .maxOrNull() ?: -1
                    val escalation = r.child("escalation")
                    val next =
                        AlarmPolicy.nextStep(
                            now,
                            due,
                            last,
                            escalation.bool("enabled") && r.bool("requires_ack"),
                            escalation.int("interval_seconds", 300),
                            escalation.int("max_repeats", 3),
                            escalation.int("max_duration_seconds", 3600),
                        ) ?: return@mapNotNull null
                    obj(
                        "occurrence_id" to row.id,
                        "version" to version,
                        "due" to due,
                        "step" to next.first,
                        "at" to maxOf(now + 500, next.second, effectiveDue),
                        "mode" to r.str("delivery_mode"),
                    )
                }
                .sortedBy { it.get("at").asLong }
                .take(AlarmPolicy.maxAlarms)
        val wanted = plans.associateBy {
            AlarmPolicy.displayKey(
                it.str("occurrence_id"),
                it.int("version"),
                it.get("due").asLong,
                it.int("step"),
            )
        }
        repo.store
            .list("alarm")
            .filter { it.id !in wanted }
            .forEach { row ->
                val a = row.json.json()
                manager.cancel(
                    intent(
                        a.str("occurrence_id"),
                        a.int("version"),
                        a.get("due").asLong,
                        a.int("step"),
                    ),
                )
                repo.store.delete("alarm", row.id)
                repo.receipt(a.str("occurrence_id"), a.int("version"), "unscheduled")
            }
        wanted.forEach { (key, a) ->
            val id = a.str("occurrence_id")
            val version = a.int("version")
            val pending = intent(id, version, a.get("due").asLong, a.int("step"))
            val at = a.get("at").asLong
            try {
                val precise = a.str("mode") != "normal" && Alerts.exact(context)
                when {
                    precise && a.str("mode") == "alarm" ->
                        manager.setAlarmClock(
                            AlarmManager.AlarmClockInfo(at, Alerts.open(context, id)),
                            pending,
                        )
                    precise ->
                        manager.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pending)
                    else -> manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pending)
                }
                val old = repo.store.get("alarm", key)
                val capable =
                    precise &&
                        Alerts.permitted(context) &&
                        context
                            .getSystemService(android.app.NotificationManager::class.java)
                            .getNotificationChannel(a.str("mode"))
                            ?.importance != 0
                a.addProperty("precise", capable)
                if (!capable && old?.json?.json()?.bool("precise") == true)
                    repo.receipt(id, version, "unscheduled")
                repo.store.put(Entity("alarm", key, a.toString()))
                // Only claim a precise local schedule when exact access is available.
                // Otherwise the server push remains a fallback and diagnostics report delay.
                if (
                    (old == null || !old.json.json().bool("precise")) &&
                        a.int("step") == 0 &&
                        capable
                )
                    repo.receipt(id, version, "scheduled")
            } catch (_: SecurityException) {
                repo.receipt(id, version, "failed", "Exact alarm access changed; open Settings")
                repo.store.delete("alarm", key)
            }
        }
    }
}
