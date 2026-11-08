#!/usr/bin/env ruby
# Generates UIFlowDriver.xcodeproj: one UI-test bundle that belongs to no app.
# Probe 3 proved this shape builds and drives any installed app by bundle id.

require 'fileutils'
require 'json'
require 'securerandom'
require 'xcodeproj'

HERE = File.expand_path(__dir__)
NAME = 'UIFlowDriver'
TARGET = 'UIFlowDriverTests'
BUNDLE_ID = 'dev.uiflow.driver.tests'
PROJECT_PATH = File.join(HERE, "#{NAME}.xcodeproj")
PLAN_PATH = File.join(HERE, "#{NAME}.xctestplan")

FileUtils.rm_rf(PROJECT_PATH)
project = Xcodeproj::Project.new(PROJECT_PATH)
target = project.new_target(:ui_test_bundle, TARGET, :ios, '18.0')

group = project.new_group(TARGET, TARGET)
sources = Dir.glob(File.join(HERE, TARGET, '*.swift')).sort
raise "no sources in #{TARGET}/" if sources.empty?
sources.each do |path|
  ref = group.new_reference(path)
  target.add_file_references([ref])
end

target.build_configurations.each do |config|
  config.build_settings.merge!(
    'TEST_TARGET_NAME' => '',
    'CODE_SIGNING_ALLOWED' => 'NO',
    'CODE_SIGNING_REQUIRED' => 'NO',
    'CODE_SIGN_IDENTITY' => '',
    'PRODUCT_BUNDLE_IDENTIFIER' => BUNDLE_ID,
    'PRODUCT_NAME' => '$(TARGET_NAME)',
    'GENERATE_INFOPLIST_FILE' => 'YES',
    'SWIFT_VERSION' => '5.0',
    'TARGETED_DEVICE_FAMILY' => '1,2',
    'IPHONEOS_DEPLOYMENT_TARGET' => '18.0',
    'SUPPORTS_MACCATALYST' => 'NO',
    'ENABLE_USER_SCRIPT_SANDBOXING' => 'NO',
    'SWIFT_EMIT_LOC_STRINGS' => 'NO'
  )
end
project.build_configurations.each do |config|
  config.build_settings['SDKROOT'] = 'iphoneos'
  config.build_settings['ONLY_ACTIVE_ARCH'] = 'YES'
end

project.save

# The test plan keeps a two-hour session from writing an attachment per synthesized tap
# (review finding 14). Xcodeproj cannot express test plans, so it is emitted and referenced by hand.
plan = {
  'configurations' => [{ 'id' => SecureRandom.uuid.upcase, 'name' => 'Configuration 1', 'options' => {} }],
  'defaultOptions' => {
    'uiTestingScreenshotsLifetime' => 'keepNever',
    'userAttachmentLifetime' => 'keepNever'
  },
  'testTargets' => [{
    'target' => {
      'containerPath' => "container:#{NAME}.xcodeproj",
      'identifier' => target.uuid,
      'name' => TARGET
    }
  }],
  'version' => 1
}
File.write(PLAN_PATH, JSON.pretty_generate(plan) + "\n")

scheme = Xcodeproj::XCScheme.new
scheme.add_test_target(target)
scheme.save_as(PROJECT_PATH, NAME, true)

scheme_path = File.join(PROJECT_PATH, 'xcshareddata', 'xcschemes', "#{NAME}.xcscheme")
xml = File.read(scheme_path)
plan_ref = "      <TestPlans>\n" \
           "         <TestPlanReference\n" \
           "            reference = \"container:#{NAME}.xctestplan\"\n" \
           "            default = \"YES\">\n" \
           "         </TestPlanReference>\n" \
           "      </TestPlans>\n"
xml = xml.sub(/(<TestAction\b[^>]*>\n)/) { "#{$1}#{plan_ref}" }
raise 'could not insert TestPlans into the scheme' unless xml.include?('TestPlanReference')
File.write(scheme_path, xml)

puts PROJECT_PATH
