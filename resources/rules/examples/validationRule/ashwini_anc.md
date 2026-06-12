---
rule_kind: validationRule
intent: block saving when an observation value falls outside the allowed numeric range
  (e.g. weight 24-90 kg)
entity_param: programEncounter
encounter_types: []
concepts:
- Weight
source_org: Ashwini
---
```js
({params, imports}) => {
  const programEncounter = params.entity;
  const validationResults = [];
 

  const weight = programEncounter.getObservationReadableValue('Weight');
 

  if (weight < 24 || weight > 90) {
  validationResults.push(
  imports.common.createValidationError('Please check the weight. It should be between 24 kg and 90 kg.')
  );
  }
 

  return validationResults;
 };
```
