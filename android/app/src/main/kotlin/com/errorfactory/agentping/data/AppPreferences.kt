package com.errorfactory.agentping.data

import android.content.Context
import androidx.datastore.preferences.core.*
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.map

private val Context.appPreferences by preferencesDataStore("app_preferences")

class AppPreferences(private val context: Context) {
    private val themeKey = stringPreferencesKey("theme")
    private val permissionKey = booleanPreferencesKey("permission_explained")
    val theme = context.appPreferences.data.map { it[themeKey] ?: "system" }
    val permissionExplained = context.appPreferences.data.map { it[permissionKey] ?: false }

    suspend fun theme(value: String) {
        require(value in setOf("system", "light", "dark"))
        context.appPreferences.edit { it[themeKey] = value }
    }

    suspend fun permissionExplained() {
        context.appPreferences.edit { it[permissionKey] = true }
    }
}
