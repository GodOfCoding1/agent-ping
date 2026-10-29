package com.errorfactory.agentping

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import kotlinx.coroutines.launch

class AlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val pending = goAsync()
        context.app.scope.launch {
            try {
                context.app.repository.fire(
                    intent.getStringExtra("id") ?: return@launch,
                    intent.getIntExtra("version", 0),
                    intent.getLongExtra("due", 0),
                    intent.getIntExtra("step", 0),
                )
            } finally {
                pending.finish()
            }
        }
    }
}

class RecoveryReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val pending = goAsync()
        context.app.scope.launch {
            try {
                context.app.repository.alarms.rebuild()
                SyncWork.enqueue(context)
            } finally {
                pending.finish()
            }
        }
    }
}

class ActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val pending = goAsync()
        context.app.scope.launch {
            try {
                context.app.repository.action(
                    intent.getStringExtra("id") ?: return@launch,
                    intent.getStringExtra("action") ?: return@launch,
                    intent.getIntExtra("minutes", 10),
                    "device_notification",
                )
            } finally {
                pending.finish()
            }
        }
    }
}
