package com.errorfactory.agentping

import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.test.core.app.ActivityScenario
import androidx.test.platform.app.InstrumentationRegistry
import com.errorfactory.agentping.data.*
import kotlinx.coroutines.runBlocking
import org.junit.Rule
import org.junit.Test

class ApplicationUiTest {
    @get:Rule val compose = createEmptyComposeRule()

    @Test
    fun cachedScreensAndManualEditorRemainUsableWhenAuthorizationFails(): Unit = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        if (InstrumentationRegistry.getArguments().getString("manualApiCheck") == "true") {
            val repo = context.app.repository
            org.junit.Assert.assertTrue("Pair the dedicated emulator first", repo.paired)
            val title = "UI integration " + java.util.UUID.randomUUID().toString().take(8)
            try {
                ActivityScenario.launch(MainActivity::class.java).use {
                    compose.onNodeWithTag("nav-Reminders").performClick()
                    compose.onNodeWithText("New reminder").performClick()
                    compose.onNodeWithText("Title").performTextInput(title)
                    compose.onNodeWithText("Save reminder").performScrollTo().performClick()
                    compose.waitUntil(30000) {
                        compose.onAllNodesWithText("Save reminder").fetchSemanticsNodes().isEmpty()
                    }
                    compose
                        .onNodeWithText("Search title, description or agent")
                        .performTextInput(title)
                    compose.waitUntil(10000) {
                        compose
                            .onAllNodes(hasText(title) and !hasSetTextAction())
                            .fetchSemanticsNodes()
                            .isNotEmpty()
                    }
                    compose.onNode(hasText(title) and !hasSetTextAction()).assertIsDisplayed()
                    org.junit.Assert.assertTrue(
                        repo.store.list("reminder").any { it.json.json().str("title") == title },
                    )
                }
            } finally {
                repo.store
                    .list("reminder")
                    .filter { it.json.json().str("title") == title }
                    .forEach { repo.api.cancel(it.id) }
            }
            return@runBlocking
        }
        val repo = context.app.repository
        val saved = context.app.secrets.token
        try {
            context.app.secrets.token = "apd_expired_ui_test_credential"
            repo.repairRequested.value = false
            repo.store.put(
                Entity(
                    "reminder",
                    "ui-reminder",
                    obj(
                            "id" to "ui-reminder",
                            "title" to "Coffee test",
                            "body" to "Cached reminder",
                            "delivery_mode" to "normal",
                            "status" to "active",
                            "revision" to 1,
                        )
                        .toString(),
                ),
            )
            ActivityScenario.launch(MainActivity::class.java).use {
                compose.onNodeWithTag("nav-Reminders").performClick()
                compose
                    .onNodeWithText("Search title, description or agent")
                    .performTextInput("Coffee test")
                compose.waitUntil(10000) {
                    compose
                        .onAllNodes(hasText("Coffee test") and !hasSetTextAction())
                        .fetchSemanticsNodes()
                        .isNotEmpty()
                }
                compose.onNode(hasText("Coffee test") and !hasSetTextAction()).assertIsDisplayed()
                compose.onNodeWithText("New reminder").performClick()
                compose.onNodeWithText("Local date and time").assertExists()
                compose.onNodeWithText("Custom").assertExists()
                compose.onNodeWithText("Cancel").performScrollTo().performClick()
                compose.onNodeWithTag("nav-Activity").performClick()
                compose.onNodeWithText("Refresh activity").assertExists()
                compose.onNodeWithTag("nav-Devices").performClick()
                compose
                    .onNodeWithText(
                        "One primary phone rings alarms by default. Other modes can be mirrored to the selected phones.",
                    )
                    .assertExists()
                compose.onNodeWithTag("nav-Settings").performClick()
                compose.onNodeWithText("Notification channels").assertExists()
            }
        } finally {
            repo.store.delete("reminder", "ui-reminder")
            context.app.secrets.token = saved
        }
    }
}
