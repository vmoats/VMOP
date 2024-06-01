#!/bin/bash

set -e

UI_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"/..
echo $UI_DIR
TEST_TEXT="(WRAPPED_SOURCE_TEXT)"
TEST_TS_FILE=$UI_DIR/translations/main_test_en.ts
TEST_QM_FILE=$UI_DIR/translations/main_test_en.qm

# translation strings
UNFINISHED="<translation type=\"unfinished\"><\/translation>"
TRANSLATED="<translation>$TEST_TEXT<\/translation>"

mkdir -p $UI_DIR/translations
lupdate -recursive "$UI_DIR" -ts $TEST_TS_FILE
sed -i "s/$UNFINISHED/$TRANSLATED/" $TEST_TS_FILE
lrelease $TEST_TS_FILE
