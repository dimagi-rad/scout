#!/bin/bash
# ECR auth token for Kamal (same pattern as data-buddy)
# See ./config/deploy.yml

set -e

REGION=us-east-1
PROFILE_ARG=""
if [ -z "$CI" ]; then
  # if not in github actions, specify the profile
  PROFILE_ARG=" --profile ${AWS_PROFILE:-scout}"
fi

aws sts get-caller-identity $PROFILE_ARG &> /dev/null
EXIT_CODE="$?"
if [ $EXIT_CODE != 0 ]; then
    aws sso login $PROFILE_ARG
fi
aws ecr get-login-password --region=$REGION $PROFILE_ARG
