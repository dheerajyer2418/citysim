@rem Gradle startup script for Windows.
@rem Generated scaffold wrapper script. Requires gradle\wrapper\gradle-wrapper.jar.

@if "%DEBUG%"=="" @echo off
set APP_BASE_NAME=%~n0
set APP_HOME=%~dp0

set DEFAULT_JVM_OPTS="-Xmx64m" "-Xms64m"
set CLASSPATH=%APP_HOME%\gradle\wrapper\gradle-wrapper.jar

if not exist "%CLASSPATH%" (
    echo Gradle wrapper jar not found: %CLASSPATH% 1>&2
    echo Generate it with a local Gradle install: gradle wrapper --gradle-version 8.10.2 1>&2
    exit /b 1
)

if defined JAVA_HOME (
    set JAVA_EXE=%JAVA_HOME%\bin\java.exe
) else (
    set JAVA_EXE=java.exe
)

"%JAVA_EXE%" %DEFAULT_JVM_OPTS% %JAVA_OPTS% %GRADLE_OPTS% ^
    -Dorg.gradle.appname=%APP_BASE_NAME% ^
    -classpath "%CLASSPATH%" ^
    org.gradle.wrapper.GradleWrapperMain %*

exit /b %ERRORLEVEL%
