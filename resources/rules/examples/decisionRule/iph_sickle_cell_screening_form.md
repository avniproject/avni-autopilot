---
rule_kind: decisionRule
intent: surface selected observations as decisions only when a gating answer (e.g.
  consent == Yes) is recorded
entity_param: encounter
encounter_types: []
concepts:
- 63e85622-dc67-4872-a96a-2032e1ec3439
- Solubility Test for SCD
source_org: IPH Sickle Cell
---
```js
"use strict";
 ({params, imports}) => {
  const encounter = params.entity;
  const decisions = params.decisions;
  let participantsConsent = encounter.getObservationReadableValue('63e85622-dc67-4872-a96a-2032e1ec3439');
  
  if (participantsConsent && participantsConsent == 'Yes') {
  let obs=encounter.getObservationReadableValue('Solubility Test for SCD');
  decisions.registrationDecisions.push({name : "Solubility Test for SCD", value :obs});
  decisions.registrationDecisions.push({name : "Solubility test date", value :encounter.encounterDateTime});
  }
  
  return decisions;
 };
```
