package com.errorfactory.agentping

import androidx.core.app.NotificationCompat

/** Phone notification bridging boundary. No independent Wear scheduler or Data Layer yet. */
object WatchBridge {
    fun extension(occurrenceId: String) =
        NotificationCompat.WearableExtender().setDismissalId(occurrenceId)
}
