package com.errorfactory.agentping

import androidx.room.Room
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.errorfactory.agentping.data.*
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DeviceFoundationTest {
    private val context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun credentialIsEncryptedAndSurvivesReopen() {
        // Dedicated test preferences would share the production key but never replace a paired
        // credential.
        val testContext = context.createDeviceProtectedStorageContext()
        val first = SecureStore(testContext)
        val saved = first.token
        try {
            first.token = "apd_instrumentation_credential"
            assertEquals("apd_instrumentation_credential", SecureStore(testContext).token)
            val raw =
                testContext.getSharedPreferences("device_identity", 0).getString("credential", "")!!
            assertFalse(raw.contains("apd_instrumentation"))
            assertEquals(first.installationId, SecureStore(testContext).installationId)
        } finally {
            first.token = saved
        }
    }

    @Test
    fun duplicateEventClaimsAndReceiptQueueSurviveDatabaseReopen() = runBlocking {
        val file = "foundation-test.db"
        context.deleteDatabase(file)
        var db = Room.databaseBuilder(context, LocalDatabase::class.java, file).build()
        try {
            assertTrue(db.store().claim(Event("event", "occurrence", 1, "displayed", 1)) != -1L)
            db.store().queue(Pending("receipt", "receipt", "{}", 1))
            db.close()
            db = Room.databaseBuilder(context, LocalDatabase::class.java, file).build()
            assertEquals(-1L, db.store().claim(Event("event", "occurrence", 1, "claimed", 2)))
            assertEquals(1, db.store().pending().size)
        } finally {
            db.close()
            context.deleteDatabase(file)
        }
    }

    @Test
    fun distinctChannelsAndPermissionDiagnostics() {
        Alerts.channels(context)
        val manager = context.getSystemService(android.app.NotificationManager::class.java)
        assertEquals(4, Alerts.modes.map { manager.getNotificationChannel(it).id }.distinct().size)
        assertTrue(Alerts.capabilities(context).has("notifications_permitted"))
    }
}
