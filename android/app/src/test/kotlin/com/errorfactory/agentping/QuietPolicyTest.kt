package com.errorfactory.agentping

import java.time.Instant
import org.junit.Assert.*
import org.junit.Test

class QuietPolicyTest {
    @Test
    fun midnightWindowsEqualTimesAndDstGapAreDeterministic() {
        assertEquals(
            Instant.parse("2026-10-30T01:30:00Z"),
            QuietPolicy.end(
                Instant.parse("2026-10-29T18:00:00Z"),
                "Asia/Kolkata",
                true,
                "22:00",
                "07:00",
            ),
        )
        assertNull(
            QuietPolicy.end(
                Instant.parse("2026-10-29T09:00:00Z"),
                "Asia/Kolkata",
                true,
                "22:00",
                "07:00",
            ),
        )
        assertNull(QuietPolicy.end(Instant.EPOCH, "UTC", true, "22:00", "22:00"))
        assertEquals(
            Instant.parse("2026-03-29T01:30:00Z"),
            QuietPolicy.end(
                Instant.parse("2026-03-29T00:00:00Z"),
                "Europe/Berlin",
                true,
                "00:00",
                "02:30",
            ),
        )
    }
}
