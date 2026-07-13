import sys
sys.path.insert(0, 'd:\\ior_assistant')

import tests.test_hypothesis_and_faiss as t

print("Running test suite...")

# Run test runner logic from the file
t.test_get_id_variations_alphanumeric()
print("Passed test_get_id_variations_alphanumeric")

t.test_get_id_variations_float_string()
print("Passed test_get_id_variations_float_string")

t.test_get_id_variations_digit_string()
print("Passed test_get_id_variations_digit_string")

t.test_get_id_variations_integer()
print("Passed test_get_id_variations_integer")

t.test_build_and_cache_small_index_mapping()
print("Passed test_build_and_cache_small_index_mapping")

t.test_profile_dataframe_date_resolution()
print("Passed test_profile_dataframe_date_resolution")

t.test_profile_dataframe_zero_loss_warning()
print("Passed test_profile_dataframe_zero_loss_warning")

t.test_profile_dataframe_side_by_side_mapping()
print("Passed test_profile_dataframe_side_by_side_mapping")

t.test_generate_dynamics_chart()
print("Passed test_generate_dynamics_chart")

t.test_ior_hypothesis_skill_registration()
print("Passed test_ior_hypothesis_skill_registration")

t.test_run_preset_async_emit()
print("Passed test_run_preset_async_emit")

t.test_run_preset_period_mapping()
print("Passed test_run_preset_period_mapping")

print("ALL TESTS OK!")
