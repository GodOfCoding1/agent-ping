package com.errorfactory.agentping

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.google.firebase.FirebaseApp
import java.net.HttpURLConnection
import java.net.URI
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class EnvironmentSmokeTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    @Test
    fun developmentApkLaunches() {
        assertEquals("com.errorfactory.agentping.debug", BuildConfig.APPLICATION_ID)
        compose.onNodeWithText("AgentPing").assertIsDisplayed()
    }

    @Test
    fun firebaseClientConfigurationLoadsWhenPresent() {
        assumeTrue(BuildConfig.FIREBASE_CONFIGURED)
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        assertNotNull(FirebaseApp.initializeApp(context))
    }

    @Test
    fun configuredBackendIsReachableWhenRequested() {
        assumeTrue(InstrumentationRegistry.getArguments().getString("backendHealthCheck") == "true")
        val connection =
            URI(BuildConfig.BACKEND_BASE_URL).resolve("health").toURL().openConnection()
                as HttpURLConnection
        try {
            connection.connectTimeout = 10_000
            connection.readTimeout = 10_000
            assertEquals(200, connection.responseCode)
        } finally {
            connection.disconnect()
        }
    }
}
