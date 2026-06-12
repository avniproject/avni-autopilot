---
rule_kind: decisionRule
intent: backfill identity-card numbers (e.g. Aadhaar, Ration Card, CMCHIS) onto the
  registration from an upstream encounter when the registration field is empty
entity_param: individual
encounter_types: []
concepts:
- Aadhar card number [HP]
- CMCHIS Card Number [HP]
- Ration Card Number [HP]
source_org: Ashwini
---
```js
"use strict";
 ({ params, imports }) => {
  const individual = params.entity;
  const decisions = params.decisions;
  const _ = imports.lodash;
  //Add Aadhaar
  const valchk = new imports.rulesConfig.RuleCondition({ individual })
  .when.valueInRegistration("43c4860f-fccf-48c9-818a-191bc0f8d0cf")
  .notDefined.matches();
  if (valchk) {
  const encounter = _.find(
  individual.encounters,
  enc => enc.encounterType.name === 'Bahmni Patient Registration'
  );
  console.log('Encounter ----->', encounter);
  const Aadhaar = encounter
  ? encounter.getObservationReadableValue('Aadhar card number [HP]')
  : null;
  console.log('Aadhaar ----->', Aadhaar);
  decisions.registrationDecisions.push({
  name: "Aadhaar ID",
  value: Aadhaar
  });
  }
  //Add Ration Card Number
  const valchkration = new imports.rulesConfig.RuleCondition({ individual })
  .when.valueInRegistration("5266b857-1421-4f00-a64c-999d856e0f63")
  .notDefined.matches();
  if (valchkration) {
  const encounter = _.find(
  individual.encounters,
  enc => enc.encounterType.name === 'Bahmni Patient Registration'
  );
  console.log('Encounter ----->', encounter);
  const ration = encounter
  ? encounter.getObservationReadableValue('Ration Card Number [HP]')
  : null;
  console.log('ration ----->', ration);
  decisions.registrationDecisions.push({
  name: "Ration Card Number",
  value: ration
  });
  }
  //Add CMCHIS Card Number
  const valchkcmchis = new imports.rulesConfig.RuleCondition({ individual })
  .when.valueInRegistration("4db892ee-b062-41df-8317-9c411f09729e")
  .notDefined.matches();
  if (valchkcmchis) {
  const encounter = _.find(
  individual.encounters,
  enc => enc.encounterType.name === 'Bahmni Patient Registration'
  );
  console.log('Encounter ----->', encounter);
  const cmchis = encounter
  ? encounter.getObservationReadableValue('CMCHIS Card Number [HP]')
  : null;
  console.log('cmchis ----->', cmchis);
  decisions.registrationDecisions.push({
  name: "CMCHIS Card Number",
  value: cmchis
  });
  }
  return decisions;
 };
```
