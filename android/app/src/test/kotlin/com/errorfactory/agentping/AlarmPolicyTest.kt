package com.errorfactory.agentping

import org.junit.Assert.*
import org.junit.Test

class AlarmPolicyTest {
    @Test
    fun terminalCancelledAndOtherDeviceNeverRing() {
        AlarmPolicy.terminal.forEach {
            assertFalse(AlarmPolicy.eligible(it, true, true, 100, 300, 100, 10))
        }
        assertFalse(AlarmPolicy.eligible("scheduled", true, false, 100, 300, 100, 10))
        assertFalse(AlarmPolicy.eligible("scheduled", false, true, 100, 300, 100, 10))
    }

    @Test
    fun latenessExpirationAndHorizonAreBounded() {
        assertTrue(AlarmPolicy.eligible("scheduled", true, true, 100, 300, 100, 0))
        assertFalse(AlarmPolicy.eligible("scheduled", true, true, 100, 300, 101, 0))
        assertFalse(AlarmPolicy.eligible("scheduled", true, true, 100, 300, 300, 10))
        assertFalse(
            AlarmPolicy.eligible(
                "scheduled",
                true,
                true,
                AlarmPolicy.horizonMillis + 1,
                Long.MAX_VALUE,
                0,
                10,
            ),
        )
    }

    @Test
    fun recurrenceInstantsAreNotRecalculatedAndEscalationIsFinite() {
        assertEquals(0 to 100L, AlarmPolicy.nextStep(0, 100, -1, false, 60, 3, 3600))
        assertNull(AlarmPolicy.nextStep(200, 100, 0, false, 60, 3, 3600))
        assertEquals(1 to 60100L, AlarmPolicy.nextStep(200, 100, 0, true, 60, 3, 3600))
        assertNull(AlarmPolicy.nextStep(200, 100, 3, true, 60, 3, 3600))
        assertNull(AlarmPolicy.nextStep(4000000, 100, 0, true, 60, 3, 3600))
    }

    @Test
    fun pushAndLocalShareDedupeKeyWhileSnoozeAndEscalationDiffer() {
        assertEquals(AlarmPolicy.displayKey("o", 1, 100, 0), AlarmPolicy.displayKey("o", 1, 100, 0))
        assertNotEquals(
            AlarmPolicy.displayKey("o", 1, 100, 0),
            AlarmPolicy.displayKey("o", 2, 100, 0),
        )
        assertNotEquals(
            AlarmPolicy.displayKey("o", 1, 100, 0),
            AlarmPolicy.displayKey("o", 1, 100, 1),
        )
    }
}
