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
                testContext
                    .getSharedPreferences(BackendScope(BuildConfig.BACKEND_BASE_URL).preferencesName, 0)
                    .getString("credential", "")!!
            assertFalse(raw.contains("apd_instrumentation"))
            assertEquals(first.installationId, SecureStore(testContext).installationId)
        } finally {
            first.token = saved
        }
    }

    @Test
    fun credentialsAndReplicaCannotCrossBackends() = runBlocking {
        val testContext = context.createDeviceProtectedStorageContext()
        val localUrl = "http://127.0.0.1:8000/isolation-test/"
        val remoteUrl = "https://example.invalid/isolation-test/"
        val local = SecureStore(testContext, localUrl)
        val remote = SecureStore(testContext, remoteUrl)
        val localDb = LocalDatabase.open(testContext, localUrl)
        val remoteDb = LocalDatabase.open(testContext, remoteUrl)
        try {
            local.token = "apd_local_only"
            local.deviceId = "local-device"
            localDb.store().put(Entity("reminder", "local-reminder", "{}"))
            localDb.store().queue(Pending("local-action", "action", "{}", 1))
            assertNull(remote.token)
            assertEquals("", remote.deviceId)
            assertNotEquals(local.installationId, remote.installationId)
            assertTrue(remoteDb.store().list("reminder").isEmpty())
            assertTrue(remoteDb.store().pending().isEmpty())
            remote.token = "apd_remote_only"
            assertEquals("apd_local_only", SecureStore(testContext, localUrl).token)
            assertEquals("apd_remote_only", SecureStore(testContext, remoteUrl).token)
        } finally {
            localDb.close()
            remoteDb.close()
            for (url in listOf(localUrl, remoteUrl)) {
                val scope = BackendScope(url)
                testContext.deleteSharedPreferences(scope.preferencesName)
                testContext.deleteDatabase(scope.databaseName)
            }
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
