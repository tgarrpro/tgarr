// tgarr for the television: the Media Center in a full-screen web view, driven by the remote.
// No libraries: what Android itself brings is enough, and the app stays under 100 kB.
plugins {
    id("com.android.application")
}

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
    buildTypes {
        release {
            isMinifyEnabled = false
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
