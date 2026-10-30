import java.net.URI
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.compose.compiler)
    alias(libs.plugins.ksp)
}

// Public Firebase client configuration only. CI can build without a Firebase project.
val firebaseEnabled = providers.gradleProperty("agentping.firebaseEnabled")
    .map(String::toBooleanStrict).getOrElse(file("src/debug/google-services.json").exists())
if (firebaseEnabled) apply(plugin = "com.google.gms.google-services")

val productionBaseUrl = "https://agentping-api-583092569828.asia-southeast1.run.app/"
val backendBaseUrl = providers.gradleProperty("agentping.backendBaseUrl")
    .orElse("http://127.0.0.1:8000/").get()
val backendUri = URI(backendBaseUrl)
require(backendUri.scheme in setOf("http", "https") && backendUri.host != null &&
    backendUri.userInfo == null && backendUri.rawQuery == null && backendUri.fragment == null &&
    backendBaseUrl.endsWith("/")) {
    "agentping.backendBaseUrl must be an HTTP(S) URL ending in / without credentials/query/fragment"
}
val quotedBackendUrl = "\"${backendBaseUrl.replace("\\", "\\\\").replace("\"", "\\\"")}\""
val localBackend = backendUri.host in setOf("localhost", "127.0.0.1", "10.0.2.2")
require(backendUri.scheme == "https" || localBackend) {
    "Remote backends must use HTTPS"
}
// Optional local signing material, deliberately separate from backend environment secrets.
val signingFile = rootProject.file("signing.properties")
val releaseSigning = Properties().apply {
    if (signingFile.isFile) signingFile.inputStream().use { load(it) }
}

android {
    namespace = "com.errorfactory.agentping"
    compileSdk = 36
    buildToolsVersion = "36.0.0"

    defaultConfig {
        applicationId = "com.errorfactory.agentping"
        minSdk = 26
        targetSdk = 36
        versionCode = 2
        versionName = "0.2.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("boolean", "FIREBASE_CONFIGURED", firebaseEnabled.toString())
    }
    signingConfigs {
        if (signingFile.isFile) {
            create("production") {
                storeFile = rootProject.file(releaseSigning.getProperty("storeFile"))
                storePassword = releaseSigning.getProperty("storePassword")
                keyAlias = releaseSigning.getProperty("keyAlias")
                keyPassword = releaseSigning.getProperty("keyPassword")
            }
        }
    }
    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-dev"
            resValue("string", "app_name", "AgentPing Dev")
            buildConfigField("String", "BACKEND_BASE_URL", quotedBackendUrl)
            buildConfigField("boolean", "LOCAL_BACKEND", localBackend.toString())
        }
        release {
            if (signingFile.isFile) signingConfig = signingConfigs.getByName("production")
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            // Release always uses the managed HTTPS origin; debug alone permits local overrides.
            buildConfigField("String", "BACKEND_BASE_URL", "\"$productionBaseUrl\"")
            buildConfigField("boolean", "LOCAL_BACKEND", "false")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
        compose = true
        buildConfig = true
        resValues = true
    }
    packaging.resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
}

ksp { arg("room.schemaLocation", "$projectDir/schemas") }

dependencies {
    implementation(libs.room.runtime)
    implementation(libs.room.ktx)
    ksp(libs.room.compiler)
    implementation(libs.work.runtime)
    implementation(libs.datastore)
    implementation(libs.retrofit)
    implementation(libs.retrofit.gson)
    implementation(libs.lifecycle)
    implementation(libs.coroutines)
    testImplementation(libs.junit)
    implementation(libs.androidx.core)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.material3)
    implementation(libs.compose.tooling.preview)
    debugImplementation(libs.compose.tooling)
    implementation(platform(libs.firebase.bom))
    implementation(libs.firebase.messaging)
    androidTestImplementation(platform(libs.compose.bom))
    androidTestImplementation(libs.compose.test)
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.test.junit)
    debugImplementation(libs.compose.test.manifest)
}
