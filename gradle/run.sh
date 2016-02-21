#!/bin/sh

# Build a JAR of updated Android models for a schema.
# Takes three command-line arguments:
# 1 - UUID of schema
# 2 - the schema, as a string
# 3 - expiry time for JAR file in redis, in seconds

# use temp file for output jar file and temp dir for input schemas
MODEL_JAR=`mktemp data/models.XXXXX.jar`
SCHEMA_SOURCE_DIR=`mktemp -d data/schemas.XXXXX`
ANDROID_KEYSTORE="data/driver.keystore"
KEYSTORE_ALIAS="driver"
TSA_URL="http://timestamp.digicert.com"
# name of json file gets used by jsonschema2pojo for the base class
DRIVER_SCHEMA_FILE="DriverSchema.json"

# clean up temporary files on exit
trap "echo 'Cleaning up...'; rm $MODEL_JAR; rm -rf $SCHEMA_SOURCE_DIR; echo 'All done!'" EXIT

set -e

# delete redis key for schema, if it exists
redis-cli -h $DRIVER_REDIS_HOST -p $DRIVER_REDIS_PORT -n $DRIVER_REDIS_JAR_DB DEL $1

echo "Going to build new model jarfile ${MODEL_JAR}..."

echo "$2" > $SCHEMA_SOURCE_DIR/${DRIVER_SCHEMA_FILE}

gradle -Dorg.gradle.native=false clean assemble dexify --project-prop outfile=$MODEL_JAR \
    --project-prop sourcedir=$SCHEMA_SOURCE_DIR --stacktrace --info

echo 'Model jarfile built.'

if [ ! -e "$ANDROID_KEYSTORE" ]; then
    echo 'No key store present for jar signing! Exiting.'
    exit -1
fi

# sign jar: http://developer.android.com/tools/publishing/app-signing.html
jarsigner -verbose -sigalg SHA1withRSA -digestalg SHA1 -keystore "$ANDROID_KEYSTORE" \
    -storepass "$DRIVER_KEYSTORE_PASSWORD" -tsa "$TSA_URL" "$MODEL_JAR" "$KEYSTORE_ALIAS"

# verfiy JAR is signed
jarsigner -verify -certs -keystore "$ANDROID_KEYSTORE" "$MODEL_JAR"

# verify jar integrity
zip -T "$MODEL_JAR"

# Check jar has a classes.dex in it. If the schema json input was not found, or if
# jsonschema2pojo could not parse it, the gradle build will succeed, but the output jar
# will only contain META-INF.
if [ `zip -sf "$MODEL_JAR" | grep classes.dex | wc -l` != 1 ]; then
    echo "Built jar file has no classes.dex. Is the schema json present and valid?"
    exit -2
fi

# put jar file contents into redis
redis-cli -h $DRIVER_REDIS_HOST -p $DRIVER_REDIS_PORT -n $DRIVER_REDIS_JAR_DB -x SET $1 < $MODEL_JAR

# set expiry on key
redis-cli -h $DRIVER_REDIS_HOST -p $DRIVER_REDIS_PORT -n $DRIVER_REDIS_JAR_DB EXPIRE $1 $3

echo "Jar contents set in redis for schema ${1}. All done!"
