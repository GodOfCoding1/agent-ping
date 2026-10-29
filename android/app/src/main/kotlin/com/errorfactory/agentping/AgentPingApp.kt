package com.errorfactory.agentping

import android.app.Application
import android.content.Context
import com.errorfactory.agentping.data.*
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

class AgentPingApp : Application() {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    val secrets by lazy { SecureStore(this) }
    val db by lazy { LocalDatabase.open(this) }
    val repository by lazy {
        Repository(
            this,
            db,
            secrets,
            api(BuildConfig.BACKEND_BASE_URL.ifBlank { "https://localhost/" }, secrets),
        )
    }

    override fun onCreate() {
        super.onCreate()
        Alerts.channels(this)
    }
}

val Context.app: AgentPingApp
    get() = applicationContext as AgentPingApp
