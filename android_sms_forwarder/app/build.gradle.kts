plugins {
    id("com.android.application")
    kotlin("android")
}

android {
    namespace = "com.smartinventory.sms"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.beiko.raspberry.notifyforwarder"
        minSdk = 23
        targetSdk = 34
        versionCode = 2
        versionName = "1.0.1"
    }

    setProperty("archivesBaseName", "raspberry-notify-forwarder")

    signingConfigs {
        getByName("debug") {
            enableV1Signing = true
            enableV2Signing = true
            enableV3Signing = true
            enableV4Signing = false
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
        debug {
            isDebuggable = true
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.work:work-runtime-ktx:2.9.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.google.android.material:material:1.11.0")
}
