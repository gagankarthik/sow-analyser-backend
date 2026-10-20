# ─── Govern real-time: AppSync Events API ─────────────────────────────────────
# Open boards, contract pages and the bell re-read within a second or two when
# anyone in the same tenant changes a contract (docs/GOVERN_ARCHITECTURE.md,
# "Real-time updates").
#
#   activity stream Lambda ──IAM (SigV4) HTTP publish──▶ /govern/<tenant>
#   browser ──Cognito ID token, WebSocket──▶ subscribe /govern/<own tenant>
#
# The messages are invalidations only ({type, contractId, action, actor, at});
# the browser re-reads through the REST API, so nothing a page shows ever
# bypasses the API's own authorization.
#
# AppSync Events resources (aws_appsync_api / aws_appsync_channel_namespace)
# need AWS provider 6.x; this stack is pinned to ~> 5.0, so the two resources
# are declared through CloudFormation. Move them to the native resources when
# the provider is upgraded.

resource "aws_cloudformation_stack" "govern_realtime" {
  name = "${local.govern_prefix}-realtime"

  template_body = jsonencode({
    AWSTemplateFormatVersion = "2010-09-09"
    Description              = "Blue-IQ Govern real-time invalidations (AppSync Events)"
    Resources = {
      Api = {
        Type = "AWS::AppSync::Api"
        Properties = {
          Name = "${local.govern_prefix}-realtime"
          EventConfig = {
            AuthProviders = [
              {
                AuthType = "AMAZON_COGNITO_USER_POOLS"
                CognitoConfig = {
                  UserPoolId       = var.cognito_user_pool_id
                  AwsRegion        = local.region
                  AppIdClientRegex = "^${var.cognito_client_id}$"
                }
              },
              { AuthType = "AWS_IAM" },
            ]
            # Browsers connect and subscribe with their Cognito ID token; only
            # IAM principals (the stream Lambda) may publish.
            ConnectionAuthModes       = [{ AuthType = "AMAZON_COGNITO_USER_POOLS" }]
            DefaultSubscribeAuthModes = [{ AuthType = "AMAZON_COGNITO_USER_POOLS" }]
            DefaultPublishAuthModes   = [{ AuthType = "AWS_IAM" }]
          }
        }
      }
      GovernNamespace = {
        Type = "AWS::AppSync::ChannelNamespace"
        Properties = {
          ApiId              = { "Fn::GetAtt" = ["Api", "ApiId"] }
          Name               = "govern"
          SubscribeAuthModes = [{ AuthType = "AMAZON_COGNITO_USER_POOLS" }]
          PublishAuthModes   = [{ AuthType = "AWS_IAM" }]
          # onSubscribe: a user may subscribe only to their own tenant's channel.
          CodeHandlers = file("${path.module}/appsync/govern_channel.js")
        }
      }
    }
    Outputs = {
      ApiId          = { Value = { "Fn::GetAtt" = ["Api", "ApiId"] } }
      ApiArn         = { Value = { "Fn::GetAtt" = ["Api", "ApiArn"] } }
      HttpDomain     = { Value = { "Fn::GetAtt" = ["Api", "Dns.Http"] } }
      RealtimeDomain = { Value = { "Fn::GetAtt" = ["Api", "Dns.Realtime"] } }
    }
  })

  tags = { Name = "${local.govern_prefix}-realtime" }
}

locals {
  govern_realtime_http_domain = aws_cloudformation_stack.govern_realtime.outputs["HttpDomain"]
  govern_realtime_namespace   = "${aws_cloudformation_stack.govern_realtime.outputs["ApiArn"]}/channelNamespace/govern"
}
