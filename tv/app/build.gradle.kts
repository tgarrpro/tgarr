// tgarr for the television: the Media Center in a full-screen web view, driven by the remote.
// No libraries: what Android itself brings is enough, and the app stays under 100 kB.
import java.util.Properties

plugins {
    id("com.android.application")
}

// Release signing. The key is not in this repository: a properties file elsewhere names it
// (storeFile, storePassword, keyAlias, keyPassword), and where that file is comes from the
// environment (TGARR_TV_SIGNING) or from local.properties (tgarr.signing=...), which git
// ignores. Without it "assembleRelease" makes an unsigned package; "assembleDebug" works
// as ever. Whoever loses the key can no longer update the installed app - keep a copy offline.
val signingFile: String? = System.getenv("TGARR_TV_SIGNING") ?: Properties().apply {
    val lp = rootProject.file("local.properties")
    if (lp.exists()) lp.inputStream().use { load(it) }
}.getProperty("tgarr.signing")
val signing = Properties().apply {
    val f = signingFile?.let { file(it) }
    if (f != null && f.exists()) f.inputStream().use { load(it) }
}
val haveReleaseKey = signing.getProperty("storeFile") != null

android {
    namespace = "me.tgarr.tv"
    compileSdk = 35

    defaultConfig {
        applicationId = "me.tgarr.tv"
        minSdk = 21            // Android TV 5.0, Fire OS 5
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }
    signingConfigs {
        if (haveReleaseKey) {
            create("release") {
                storeFile = file(signing.getProperty("storeFile"))
                storePassword = signing.getProperty("storePassword")
                keyAlias = signing.getProperty("keyAlias")
                keyPassword = signing.getProperty("keyPassword")
                storeType = "PKCS12"
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            if (haveReleaseKey) signingConfig = signingConfigs.getByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
    lint {
        abortOnError = false
    }
}
