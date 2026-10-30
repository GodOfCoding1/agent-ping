package com.errorfactory.agentping

import com.errorfactory.agentping.data.BackendScope
import org.junit.Assert.*
import org.junit.Test

class BackendScopeTest {
    @Test
    fun schemeHostPortAndPathSeparateAccounts() {
        val urls = listOf(
            "http://127.0.0.1:8000/",
            "http://127.0.0.1:8001/",
            "https://example.com/",
            "https://example.com/other/",
            "https://other.example.com/",
        )
        assertEquals(urls.size, urls.map { BackendScope(it).preferencesName }.toSet().size)
        assertEquals(urls.size, urls.map { BackendScope(it).databaseName }.toSet().size)
    }

    @Test
    fun equivalentHttpsOriginsKeepIdentityAndLegacyFilesAreNotReused() {
        val scope = BackendScope("https://example.com/")
        assertEquals(scope.preferencesName, BackendScope("https://EXAMPLE.com:443/").preferencesName)
        assertNotEquals("device_identity", scope.preferencesName)
        assertNotEquals("agentping.db", scope.databaseName)
    }
}
