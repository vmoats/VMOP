pipeline {
  agent {
    docker {
      image 'python:3.7.3'
      args '--user=root'
    }

  }
  environment {
    COMMA_JWT = credentials('athena-test-jwt')
  }
  stages {
    parallel {
      stage('EON Build/Test') {
        steps {
          lock(resource: "", label: 'eon', inversePrecedence: true, variable: 'eon_name', quantity: 1){
            timeout(time: 30, unit: 'MINUTES') {
              dir(path: 'release') {
                sh 'pip install paramiko'
                sh 'python remote_build.py'
              }
            }
          }
        }
      }
      stage('EON Replay Tests') {
        steps {
          lock(resource: "", label: 'eon2', inversePrecedence: true, variable: 'eon_name', quantity: 1){
            timeout(time: 90, unit: 'MINUTES') {
              dir(path: 'selfdrive/test') {
                sh 'pip install paramiko'
                sh 'python phone_ci.py'
              }
            }
          }
        }
      }
    }
  }
}
