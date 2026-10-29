package com.errorfactory.agentping

/** Android-independent policy. The server expands RRULEs; the phone schedules UTC instants. */
object AlarmPolicy {
    val terminal = setOf("acknowledged", "skipped", "cancelled", "superseded", "missed")
    const val horizonMillis = 7 * 24 * 60 * 60 * 1000L
    const val maxAlarms = 64

    fun eligible(
        state: String,
        active: Boolean,
        targeted: Boolean,
        due: Long,
        expires: Long,
        now: Long,
        graceMinutes: Int,
    ): Boolean =
        state !in terminal &&
            active &&
            targeted &&
            expires > now &&
            due <= now + horizonMillis &&
            (due >= now || now - due <= graceMinutes * 60_000L)

    fun displayKey(id: String, version: Int, due: Long, step: Int) = "$id:$version:$due:$step"

    fun nextStep(
        now: Long,
        due: Long,
        lastStep: Int,
        enabled: Boolean,
        intervalSeconds: Int,
        maxRepeats: Int,
        maxDurationSeconds: Int,
    ): Pair<Int, Long>? {
        if (lastStep < 0) return 0 to due
        if (!enabled || lastStep >= maxRepeats) return null
        val next = lastStep + 1
        val at = due + intervalSeconds * 1000L * next
        return if (
            at - due <= maxDurationSeconds * 1000L && now <= due + maxDurationSeconds * 1000L
        )
            next to maxOf(now, at)
        else null
    }
}
