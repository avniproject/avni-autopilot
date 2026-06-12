---
rule_kind: decisionRule
intent: record the subject's current medication regimen (medicines and dosage details)
  as decisions, carrying values forward from prior visits when the regimen continues
  unchanged
entity_param: programEncounter
encounter_types: []
concepts:
- Sickle cell medicines
source_org: JSSCP
---
```js
"use strict";
 ({params, imports}) => {
  const programEncounter = params.entity;
  const decisions = params.decisions;
  function addConceptValueToDecision(conceptName) {
  let observationValue;
  if (medicalStatus === 'Continue same medicines') {
  observationValue = programEncounter.programEnrolment.findObservationValueInEntireEnrolment(conceptName, false);
  observationValue = observationValue ? observationValue.value : undefined;
  } else {
  observationValue = programEncounter.observations.filter(obj => obj.concept.name === conceptName)[0]
  observationValue = observationValue ? observationValue.getReadableValue() : undefined;
  }
  if (!_.isEmpty(observationValue)) {
  decisions.encounterDecisions.push({name: conceptName, value: observationValue})
  }
  }
 

  const medComplicationsBuilder = new imports.rulesConfig.complicationsBuilder({
  programEncounter: programEncounter,
  complicationsConcept: 'Sickle cell medicines'
  });
 

  let medicalStatus = programEncounter.observations.filter(obj => obj.concept.name === 'Medicine status')[0].getReadableValue();
 

  const medTakenDecisionConceptMap = {
  'Hydroxyurea': ['Hydroxyurea', 'Hydroxyurea for how many times a day', 'Hydroxyurea for how many times a day'],
  'Folic acid': ['Folic acid', 'Folic acid for how many times a day', 'Folic acid for how many days'],
  'Paracetamol': ['Paracetamol', 'Paracetamol for how many times a day', 'Paracetamol for how many days'],
  'Ibuprofen': ['Ibuprofen', 'Ibuprofen for how many times a day', 'Ibuprofen for how many days']
  };
 

  let medTaken;
  if (medicalStatus === 'Continue same medicines') {
  medTaken = programEncounter.programEnrolment.findLatestObservationInEntireEnrolment('Sickle cell medicines', false);
  medTaken = medTaken ? medTaken.getReadableValue() : undefined;
  } else {
  medTaken = programEncounter.observations.filter(obj => obj.concept.name === 'Sickle cell medicines').map(item => item.getReadableValue())[0];
  }
 

  if (medTaken) {
  _.forEach(medTaken, med => {
  medComplicationsBuilder.addComplication(med)
  });
  }
 

  decisions.encounterDecisions.push(medComplicationsBuilder.getComplications());
 

  _.forEach(medTakenDecisionConceptMap, (conceptNames, medName) => {
  if (_.includes(medTaken, medName)) {
  _.forEach(conceptNames, conceptName => {
  addConceptValueToDecision(conceptName)
  })
  }
  });
 

  return decisions;
 };
```
