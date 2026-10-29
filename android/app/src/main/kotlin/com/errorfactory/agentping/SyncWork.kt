package com.errorfactory.agentping

import android.content.Context
import androidx.work.*
import java.util.concurrent.TimeUnit

class SyncWork(context: Context, parameters: WorkerParameters) :
    CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        return if (applicationContext.app.repository.refresh()) Result.success() else Result.retry()
    }

    companion object {
        private val network =
            Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        fun enqueue(context: Context) {
            WorkManager.getInstance(context)
                .enqueueUniqueWork(
                    "sync",
                    ExistingWorkPolicy.REPLACE,
                    OneTimeWorkRequestBuilder<SyncWork>()
                        .setConstraints(network)
                        .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                        .build(),
                )
        }

        fun periodic(context: Context) {
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork(
                    "reconcile",
                    ExistingPeriodicWorkPolicy.KEEP,
                    PeriodicWorkRequestBuilder<SyncWork>(6, TimeUnit.HOURS)
                        .setConstraints(network)
                        .build(),
                )
        }
    }
}
