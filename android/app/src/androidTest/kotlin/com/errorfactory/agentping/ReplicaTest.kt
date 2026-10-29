package com.errorfactory.agentping

import androidx.room.Room
import androidx.test.platform.app.InstrumentationRegistry
import com.errorfactory.agentping.data.*
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class ReplicaTest {
    private val context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun snapshot(version: Int, cursor: Int, full: Boolean = false) =
        """{
        "full":$full,"cursor":$cursor,"server_time":"2026-10-29T09:00:00Z","horizon_end":"2026-11-29T09:00:00Z",
        "reminders":[{"id":"r","revision":1,"title":"Offline reminder"}],
        "occurrences":[{"id":"o","version":$version,"reminder_id":"r","state":"scheduled","scheduled_at":"2026-10-29T09:00:00Z"}],
        "devices":[],"settings":null
    }"""
            .json()

    @Test
    fun syncCursorAndReplicaRollbackTogetherAndIgnoreOlderVersions() = runBlocking {
        val db = Room.inMemoryDatabaseBuilder(context, LocalDatabase::class.java).build()
        try {
            val replica = Replica(db)
            replica.apply(snapshot(2, 4, true))
            replica.apply(snapshot(1, 5))
            assertEquals(2, db.store().get("occurrence", "o")!!.json.json().int("version"))
            val broken = snapshot(3, 6, true).apply { array("occurrences").add("bad schema") }
            try {
                replica.apply(broken)
                fail("must roll back")
            } catch (_: IllegalStateException) {}
            assertEquals(5, db.store().get("meta", "sync")!!.json.json().int("cursor"))
            assertEquals(2, db.store().get("occurrence", "o")!!.json.json().int("version"))
        } finally {
            db.close()
        }
    }

    @Test
    fun offlineActionIsDurableAndFullSyncCannotOverwriteIt() = runBlocking {
        val db = Room.inMemoryDatabaseBuilder(context, LocalDatabase::class.java).build()
        try {
            val replica = Replica(db)
            replica.apply(snapshot(1, 1, true))
            val secrets = SecureStore(context)
            val repo = Repository(context, db, secrets, api("http://127.0.0.1:1/", secrets))
            repo.action("o", "acknowledge")
            assertEquals(1, db.store().pending().count { it.kind == "action" })
            assertEquals(
                "acknowledged",
                db.store().get("occurrence", "o")!!.json.json().str("state"),
            )
            replica.apply(snapshot(2, 2, true))
            assertEquals(
                "acknowledged",
                db.store().get("occurrence", "o")!!.json.json().str("state"),
            )
            repo.action("o", "acknowledge")
            assertEquals(1, db.store().pending().count { it.kind == "action" })
        } finally {
            db.close()
        }
    }

    @Test
    fun missedOccurrenceCanBeAcknowledgedButCannotBeSnoozed(): Unit = runBlocking {
        val db = Room.inMemoryDatabaseBuilder(context, LocalDatabase::class.java).build()
        try {
            Replica(db)
                .apply(
                    snapshot(1, 1, true).apply {
                        array("occurrences")[0].asJsonObject.addProperty("state", "missed")
                    },
                )
            val secrets = SecureStore(context)
            val repo = Repository(context, db, secrets, api("http://127.0.0.1:1/", secrets))
            repo.action("o", "snooze", 5)
            assertEquals("missed", db.store().get("occurrence", "o")!!.json.json().str("state"))
            assertTrue(db.store().pending().isEmpty())
            repo.action("o", "acknowledge")
            assertEquals(
                "acknowledged",
                db.store().get("occurrence", "o")!!.json.json().str("state"),
            )
            assertEquals(1, db.store().pending().size)
        } finally {
            db.close()
        }
    }
}
