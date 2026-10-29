package com.errorfactory.agentping

import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import kotlinx.coroutines.runBlocking

class MessagingService : FirebaseMessagingService() {
    override fun onRegistered(installationId: String) {
        app.secrets.fid = installationId
        SyncWork.enqueue(this)
    }

    override fun onMessageReceived(message: RemoteMessage) {
        // No networking in the FCM callback. Durable local work precedes the retry worker.
        runBlocking {
            try {
                app.repository.receive(message.data)
            } catch (_: Exception) {
                app.repository.status.value =
                    "A push could not be processed; synchronization will reconcile it"
            }
        }
        SyncWork.enqueue(this)
    }

    override fun onDeletedMessages() {
        SyncWork.enqueue(this)
    }
}
