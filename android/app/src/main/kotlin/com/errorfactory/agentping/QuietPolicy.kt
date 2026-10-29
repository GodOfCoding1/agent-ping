package com.errorfactory.agentping

import java.time.*

object QuietPolicy {
    fun end(at: Instant, timezone: String, enabled: Boolean, start: String, end: String): Instant? {
        if (!enabled) return null
        val from = LocalTime.parse(start)
        val until = LocalTime.parse(end)
        if (from == until) return null
        val local = at.atZone(ZoneId.of(timezone))
        val clock = local.toLocalTime()
        val inside =
            if (from < until) clock >= from && clock < until else clock >= from || clock < until
        if (!inside) return null
        val date = local.toLocalDate().plusDays(if (from > until && clock >= from) 1 else 0)
        return date.atTime(until).atZone(ZoneId.of(timezone)).toInstant()
    }
}
