package com.errorfactory.agentping

import androidx.test.platform.app.InstrumentationRegistry
import com.errorfactory.agentping.data.*
import java.time.Instant
import java.util.UUID
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test

class LocalAlarmTest {
    @Test
    fun alarmManagerDeliversWithoutServerThenOfflineSnoozeAndDismissStopRinging() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val explicit =
            InstrumentationRegistry.getArguments().getString("alarmDeliveryCheck") == "true"
        if (explicit) {
            val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
            automation
                .executeShellCommand(
                    "pm grant ${context.packageName} android.permission.POST_NOTIFICATIONS",
                )
                .use { android.os.ParcelFileDescriptor.AutoCloseInputStream(it).readBytes() }
            automation
                .executeShellCommand("appops set ${context.packageName} SCHEDULE_EXACT_ALARM allow")
                .use { android.os.ParcelFileDescriptor.AutoCloseInputStream(it).readBytes() }
            assertTrue(Alerts.permitted(context) && Alerts.exact(context))
        } else assumeTrue(Alerts.permitted(context) && Alerts.exact(context))
        val repo = context.app.repository
        val rid = UUID.randomUUID().toString()
        val oid = UUID.randomUUID().toString()
        val due = Instant.now().plusSeconds(3)
        try {
            repo.store.put(
                Entity(
                    "reminder",
                    rid,
                    obj(
                            "id" to rid,
                            "revision" to 1,
                            "status" to "active",
                            "title" to "Offline alarm test",
                            "delivery_mode" to "alarm",
                            "late_grace_minutes" to 10,
                            "requires_ack" to true,
                        )
                        .toString(),
                ),
            )
            repo.store.put(
                Entity(
                    "occurrence",
                    oid,
                    obj(
                            "id" to oid,
                            "reminder_id" to rid,
                            "version" to 1,
                            "state" to "scheduled",
                            "scheduled_at" to due.toString(),
                            "expires_at" to due.plusSeconds(3600).toString(),
                            "deliver_on_this_device" to true,
                        )
                        .toString(),
                ),
            )
            // Recover a process that died after claiming the event, before posting it.
            val claimedKey = AlarmPolicy.displayKey(oid, 1, due.toEpochMilli(), 0)
            repo.store.claim(Event(claimedKey, oid, 1, "claimed", System.currentTimeMillis()))
            repo.alarms.rebuild()
            assertTrue(repo.store.list("alarm").any { it.json.json().str("occurrence_id") == oid })
            repeat(40) { if (RingingService.active != oid) delay(250) }
            assertEquals(oid, RingingService.active)
            assertEquals(
                1,
                context
                    .getSystemService(android.app.NotificationManager::class.java)
                    .activeNotifications
                    .count { it.notification.extras.getString("agentping_occurrence") == oid },
            )
            val key = AlarmPolicy.displayKey(oid, 1, due.toEpochMilli(), 0)
            assertTrue(repo.store.events().any { it.id == key && it.phase == "displayed" })
            repo.fire(oid, 1, due.toEpochMilli(), 0)
            assertEquals(1, repo.store.events().count { it.id == key })
            repo.action(oid, "snooze", 1)
            repeat(20) { if (RingingService.active == oid) delay(100) }
            assertNotEquals(oid, RingingService.active)
            assertEquals("snoozed", repo.store.get("occurrence", oid)!!.json.json().str("state"))
            assertTrue(repo.store.list("alarm").any { it.json.json().str("occurrence_id") == oid })
            repo.action(oid, "skip")
            assertFalse(repo.store.list("alarm").any { it.json.json().str("occurrence_id") == oid })
        } finally {
            RingingService.stop(context, oid)
            Alerts.cancel(context, oid)
            repo.alarms.cancel(oid)
            repo.store.delete("reminder", rid)
            repo.store.delete("occurrence", oid)
            repo.store.delete("push", oid)
            repo.store
                .pending()
                .filter { it.json.json().str("occurrence_id") == oid }
                .forEach { repo.store.dequeue(it.id) }
        }
    }
}
