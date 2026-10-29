import java.net.URI

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.compose.compiler)
    alias(libs.plugins.ksp)
}

// Public Firebase client configuration only. CI can build without a Firebase project.
val firebaseEnabled = providers.gradleProperty("agentping.firebaseEnabled")
    .map(String::toBooleanStrict).getOrElse(file("src/debug/google-services.json").exists())
if (firebaseEnabled) apply(plugin = "com.google.gms.google-services")

val backendBaseUrl = providers.gradleProperty("agentping.backendBaseUrl")
    .orElse("http://127.0.0.1:8000/").get()
val backendUri = URI(backendBaseUrl)
require(backendUri.scheme in setOf("http", "https") && backendUri.host != null &&
    backendUri.userInfo == null && backendUri.rawQuery == null && backendUri.fragment == null &&
    backendBaseUrl.endsWith("/")) {
    "agentping.backendBaseUrl must be an HTTP(S) URL ending in / without credentials/query/fragment"
}
val quotedBackendUrl = "\"${backendBaseUrl.replace("\\", "\\\\").replace("\"", "\\\"")}\""

android {
    namespace = "com.errorfactory.agentping"
    compileSdk = 36
    buildToolsVersion = "36.0.0"

    defaultConfig {
        applicationId = "com.errorfactory.agentping"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("boolean", "FIREBASE_CONFIGURED", firebaseEnabled.toString())
    }
    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-dev"
            resValue("string", "app_name", "AgentPing Dev")
            buildConfigField("String", "BACKEND_BASE_URL", quotedBackendUrl)
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            // A release endpoint/authentication flow belongs to a later implementation stage.
            buildConfigField("String", "BACKEND_BASE_URL", "\"\"")
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
